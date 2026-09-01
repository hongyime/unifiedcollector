"""wa_phone_intel — OFFLINE phone-number OSINT enrichment for WhatsApp numbers.

Parses the digits behind every `whatsapp_lid_map.phone_jid` through the
`phonenumbers` library (pure-Python, offline dataset — NO network, NO API key)
and upserts region / carrier / line-type / timezone metadata into `wa_phone_intel`.

ENRICHMENT-ONLY. Carrier / region do NOT identify individual people, so these
rows are NEVER written to `identity_signals` and MUST NOT be consumed by the
unifiedanalyzer merge scorer. Same policy as `wa_devices`.

Run inside a collector container that has the `phonenumbers` package installed
(added to requirements.txt) and DATABASE_URL pointing at the collector DB:

    python -m src.core.wa_phone_intel --limit 100 [--dry-run]

`--limit` bounds one invocation; the full 17k backfill is an OPERATOR action:

    python -m src.core.wa_phone_intel --limit 20000        # full sweep

The module mirrors src/core/wa_device_sweep.py minimally: batch-select least-
recently-enriched numbers from whatsapp_lid_map, parse, upsert, jittered
spacing. Idempotent — safe to re-run.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from typing import Any

import asyncpg

try:
    import phonenumbers
    from phonenumbers import carrier as pn_carrier
    from phonenumbers import geocoder as pn_geocoder
    from phonenumbers import timezone as pn_timezone
    from phonenumbers.phonenumberutil import NumberParseException, PhoneNumberType
except ImportError as _e:  # pragma: no cover — surfaced at CLI time
    phonenumbers = None  # type: ignore[assignment]
    _PN_IMPORT_ERROR: BaseException | None = _e
else:
    _PN_IMPORT_ERROR = None

logger = logging.getLogger(__name__)

BATCH = int(os.getenv("WA_PHONE_INTEL_BATCH", "100"))

# phonenumbers.PhoneNumberType is an IntEnum-ish; map back to readable strings so
# the DB column stays human-inspectable (no magic integers).
_LINE_TYPE_NAMES: dict[int, str] = {
    PhoneNumberType.FIXED_LINE: "FIXED_LINE",
    PhoneNumberType.MOBILE: "MOBILE",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "FIXED_LINE_OR_MOBILE",
    PhoneNumberType.TOLL_FREE: "TOLL_FREE",
    PhoneNumberType.PREMIUM_RATE: "PREMIUM_RATE",
    PhoneNumberType.SHARED_COST: "SHARED_COST",
    PhoneNumberType.VOIP: "VOIP",
    PhoneNumberType.PERSONAL_NUMBER: "PERSONAL_NUMBER",
    PhoneNumberType.PAGER: "PAGER",
    PhoneNumberType.UAN: "UAN",
    PhoneNumberType.VOICEMAIL: "VOICEMAIL",
    PhoneNumberType.UNKNOWN: "UNKNOWN",
} if phonenumbers is not None else {}


def _dsn() -> str:
    return os.environ["DATABASE_URL"]


def _extract_digits(phone_jid: str) -> str:
    """Return just the leading digits of a WhatsApp JID.

    "6591234567@s.whatsapp.net" -> "6591234567"
    "573012309157"              -> "573012309157"
    """
    return re.sub(r"[^0-9]", "", phone_jid.split("@")[0])


def _parse_one(phone_jid: str) -> dict[str, Any]:
    """Parse one JID with phonenumbers. Never raises — returns a row dict.

    is_valid=False rows are still upserted so we don't re-parse them next run,
    but they carry NULL enrichment.
    """
    digits = _extract_digits(phone_jid)
    row: dict[str, Any] = {
        "phone_jid": phone_jid,
        "e164": None,
        "country_code": None,
        "region": None,
        "region_name": None,
        "carrier": None,
        "line_type": None,
        "timezones": [],
        "is_valid": False,
    }
    if not digits:
        return row
    # phonenumbers requires an "+" or a default region hint; WhatsApp phone_jids
    # are always E.164 without the plus, so prepend it.
    try:
        num = phonenumbers.parse("+" + digits, None)
    except NumberParseException:
        return row
    is_valid = bool(phonenumbers.is_valid_number(num))
    row["is_valid"] = is_valid
    row["country_code"] = int(num.country_code) if num.country_code else None
    try:
        row["e164"] = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        row["e164"] = None
    region = phonenumbers.region_code_for_number(num) or None
    row["region"] = region
    # Description in English (offline dataset).
    try:
        region_name = pn_geocoder.description_for_number(num, "en") or None
    except Exception:
        region_name = None
    row["region_name"] = region_name
    try:
        row["carrier"] = pn_carrier.name_for_number(num, "en") or None
    except Exception:
        row["carrier"] = None
    try:
        row["line_type"] = _LINE_TYPE_NAMES.get(phonenumbers.number_type(num), "UNKNOWN")
    except Exception:
        row["line_type"] = None
    try:
        tzs = pn_timezone.time_zones_for_number(num) or ()
        row["timezones"] = [t for t in tzs if t and t != "Etc/Unknown"]
    except Exception:
        row["timezones"] = []
    return row


async def _select_batch(conn: asyncpg.Connection, limit: int) -> list[str]:
    """Least-recently-enriched numbers first: unseen jids, then oldest updated_at."""
    rows = await conn.fetch(
        """
        SELECT m.phone_jid
        FROM whatsapp_lid_map m
        LEFT JOIN wa_phone_intel p ON p.phone_jid = m.phone_jid
        WHERE m.phone_jid LIKE '%@s.whatsapp.net'
        ORDER BY p.updated_at ASC NULLS FIRST
        LIMIT $1
        """,
        limit,
    )
    return [r["phone_jid"] for r in rows]


async def _upsert(conn: asyncpg.Connection, row: dict[str, Any]) -> None:
    await conn.execute(
        """
        INSERT INTO wa_phone_intel (
            phone_jid, e164, country_code, region, region_name,
            carrier, line_type, timezones, is_valid, updated_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, now())
        ON CONFLICT (phone_jid) DO UPDATE SET
            e164         = EXCLUDED.e164,
            country_code = EXCLUDED.country_code,
            region       = EXCLUDED.region,
            region_name  = EXCLUDED.region_name,
            carrier      = EXCLUDED.carrier,
            line_type    = EXCLUDED.line_type,
            timezones    = EXCLUDED.timezones,
            is_valid     = EXCLUDED.is_valid,
            updated_at   = now()
        """,
        row["phone_jid"], row["e164"], row["country_code"],
        row["region"], row["region_name"],
        row["carrier"], row["line_type"],
        row["timezones"], row["is_valid"],
    )


async def run(limit: int, dry_run: bool = False) -> dict[str, int]:
    if phonenumbers is None:
        raise RuntimeError(
            f"phonenumbers library not installed ({_PN_IMPORT_ERROR!r}); "
            "add `phonenumbers` to requirements.txt and rebuild/pip-install."
        )
    conn = await asyncpg.connect(_dsn(), command_timeout=60)
    try:
        batch = await _select_batch(conn, limit)
        print(f"[wa_phone_intel] batch={len(batch)} dry_run={dry_run}")
        ok = valid = invalid = 0
        for phone_jid in batch:
            row = _parse_one(phone_jid)
            if row["is_valid"]:
                valid += 1
            else:
                invalid += 1
            if dry_run:
                print(f"  [dry] {phone_jid} -> region={row['region']} carrier={row['carrier']} line={row['line_type']}")
                continue
            await _upsert(conn, row)
            ok += 1
        print(f"[wa_phone_intel] done upserts={ok} valid={valid} invalid={invalid}")
        return {"upserts": ok, "valid": valid, "invalid": invalid, "batch": len(batch)}
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline phone-OSINT enrichment for whatsapp_lid_map")
    ap.add_argument("--limit", type=int, default=BATCH,
                    help=f"batch size (default {BATCH})")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and print, do not upsert")
    args = ap.parse_args()
    asyncio.run(run(args.limit, args.dry_run))


if __name__ == "__main__":
    main()
