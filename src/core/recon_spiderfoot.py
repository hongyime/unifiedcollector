from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import signal
import tempfile
import secrets
import string
import shlex
import shutil
from pathlib import Path
from typing import Any

from src.core.recon_egress_guard import (
    UnsafeReconTarget,
    network_policy_installed,
    validate_recon_target,
)
from src.core.recon_evidence import ensure_evidence_capacity, store_private_evidence
from src.core.recon_rollout import (
    AdmissionDenied,
    AdmissionPolicyError,
    admission_snapshot,
    commit_claim,
    halt_admission,
    release_claim,
    reserve_claim,
    validate_zero_spend_modules,
)

DEFAULT_MODULES = (
    "sfp_dnsresolve",
    "sfp_whois",
    "sfp_names",
    "sfp_dnsbrute",
    "sfp_portscan_tcp",
)

# SpiderFoot 4.0 (the pinned image version) does not ship this module. Fail
# explicitly if a config requests it instead of letting the CLI ignore it.
UNAVAILABLE_MODULES = frozenset({"sfp_portscan_udp"})

# SpiderFoot 4.0 labels these sources commercial/private-only, or they have
# explicit provider-specific billing/usage uncertainty despite a limited free
# tier. Refuse an explicit request so a free module is never silently dropped
# and a paid module can never enter the effective command by environment or
# per-target override.
PAID_OR_UNCERTAIN_MODULES = frozenset(
    {
        "sfp_badpackets",
        "sfp_c99",
        "sfp_censys",
        "sfp_dehashed",
        "sfp_fsecure_riddler",
        "sfp_haveibeenpwned",
        "sfp_projectdiscovery",
        "sfp_seon",
        "sfp_shodan",
        "sfp_sociallinks",
        "sfp_spur",
        "sfp_whoisology",
        "sfp_whoxy",
    }
)

# maigret: HTTP-only OSS account-enumeration engine (MIT). Replaces the DEAD
# sfp_accounts module for username targets. Routed here when a target's scope
# has `modules=['maigret']` (per-target opt-in, used by the pilot) OR when the
# RECON_USERNAME_ENGINE env is set to "maigret" globally.
MAIGRET_MODULE = "maigret"
MAIGRET_SOURCE_HEALTH_NAME = "maigret"
GHUNT_MODULE = "ghunt"
GHUNT_OBSERVATION_TYPE = "GOOGLE_ACCOUNT"
DEFAULT_MAIGRET_TOP_SITES = "300"
DEFAULT_MAIGRET_NUM_REQUESTS = "20"
DEFAULT_MAIGRET_HTTP_TIMEOUT_SECONDS = "10"
DEFAULT_MAIGRET_TARGET_TIMEOUT_SECONDS = "420"
SOURCE_HEALTH_NAME = "spiderfoot"
SUPPORTED_TYPES = {"domain", "ip", "ipv4", "email", "username", "user", "url", "phone"}
def allowed_modules(raw: str | None = None) -> list[str]:
    modules = [item.strip() for item in (raw or os.getenv("SPIDERFOOT_MODULES") or "").split(",") if item.strip()]
    modules = modules or list(DEFAULT_MODULES)
    unavailable = sorted(set(modules) & UNAVAILABLE_MODULES)
    if unavailable:
        raise ValueError(
            "modules are unavailable in the installed SpiderFoot version: "
            + ", ".join(unavailable)
        )
    uncertain = sorted(set(modules) & PAID_OR_UNCERTAIN_MODULES)
    if uncertain:
        raise ValueError(
            "modules excluded by zero-spend policy: " + ", ".join(uncertain)
        )
    try:
        max_modules = max(1, int(os.getenv("SPIDERFOOT_MAX_MODULES", "10")))
    except ValueError:
        max_modules = 10
    if len(modules) > max_modules:
        raise ValueError(
            f"{len(modules)} modules requested but SPIDERFOOT_MAX_MODULES is {max_modules}; "
            "raise the limit or explicitly reduce the requested module set"
        )
    return modules


def effective_modules_for_target(override: list[str] | str | None = None) -> list[str]:
    """Add target-specific modules without letting an override narrow coverage.

    Maigret and GHunt are separate engines, so their sole-engine selectors stay
    isolated and are handled by the existing routing branches.
    """
    if isinstance(override, str):
        requested = [item.strip() for item in override.split(",") if item.strip()]
    elif isinstance(override, list):
        requested = [str(item).strip() for item in override if str(item).strip()]
    else:
        requested = []
    normalized = [item.lower() for item in requested]
    if normalized in ([MAIGRET_MODULE], [GHUNT_MODULE]):
        return normalized
    if MAIGRET_MODULE in normalized or GHUNT_MODULE in normalized:
        raise ValueError("Maigret/GHunt must be selected as the sole target engine")

    modules = allowed_modules()
    for module in requested:
        if module not in modules:
            modules.append(module)
    return allowed_modules(",".join(modules))


def _json_dict(value: Any) -> dict[str, Any]:
    """Read per-target engine options; this is not an admission policy."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def stale_target_minutes() -> int:
    try:
        return max(5, int(os.getenv("SPIDERFOOT_STALE_TARGET_MINUTES", "120")))
    except ValueError:
        return 120


def spiderfoot_max_threads() -> int:
    try:
        return max(1, min(int(os.getenv("SPIDERFOOT_MAX_THREADS", "4")), 20))
    except ValueError:
        return 4


def _bounded_timeout_seconds(name: str, default: int, maximum: int) -> int:
    try:
        return min(maximum, max(1, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def _safe_worker_label(worker_label: int | str | None) -> str:
    if worker_label is None:
        return "default"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(worker_label))
    return (safe or "default")[:32]


def _spiderfoot_env(worker_label: int | str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    state_root = Path(os.getenv("SPIDERFOOT_STATE_ROOT", "/tmp/spiderfoot-state"))
    home = state_root / _safe_worker_label(worker_label)
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    return env


def _is_target_level_failure(error: str) -> bool:
    return (
        error.startswith("SpiderFoot timed out after ")
        or "Could not determine target type" in error
        or "Invalid target" in error
    )


def _extract_json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    rows: list[dict[str, Any]] = []
    position = 0
    while position < len(text):
        next_object = text.find("{", position)
        if next_object < 0:
            break
        try:
            value, end = decoder.raw_decode(text[next_object:])
        except json.JSONDecodeError:
            position = next_object + 1
            continue
        if isinstance(value, dict):
            rows.append(value)
        position = next_object + max(end, 1)
    return rows


def parse_spiderfoot_stdout(stdout: bytes) -> list[dict[str, Any]]:
    text = stdout.decode("utf-8", errors="replace")
    try:
        return normalize_spiderfoot_payload(json.loads(text))
    except json.JSONDecodeError as exc:
        rows = _extract_json_objects(text)
        if rows:
            return rows
        raise RuntimeError(f"SpiderFoot returned malformed JSON output at byte {exc.pos}") from exc


async def _mark_source_health(conn, status: str, error: str | None = None, *, success: bool = False) -> None:
    await conn.execute(
        """
        INSERT INTO source_health (source, status, last_success_at, last_error, updated_at)
        VALUES ($1, $2, CASE WHEN $4 THEN NOW() ELSE NULL END, $3, NOW())
        ON CONFLICT (source) DO UPDATE SET
            status = EXCLUDED.status,
            last_success_at = CASE
                WHEN $4 THEN NOW()
                ELSE source_health.last_success_at
            END,
            last_error = EXCLUDED.last_error,
            updated_at = NOW()
        """,
        SOURCE_HEALTH_NAME,
        status,
        error,
        success,
    )


async def _fail_empty_target(
    conn,
    target: dict[str, Any],
    engine: str,
    *,
    modules: list[str] | None = None,
) -> dict[str, Any]:
    error = f"{engine} scan produced no useful observations"
    await conn.execute(
        "UPDATE recon_targets SET status = 'failed', error = $2, updated_at = NOW() WHERE id = $1::uuid",
        target["id"],
        error,
    )
    await _mark_source_health(conn, "degraded", error)
    result: dict[str, Any] = {
        "status": "failed",
        "target": target,
        "observations": 0,
        "error": error,
        "dry_run": False,
    }
    if modules is not None:
        result["modules"] = modules
    if engine != SOURCE_HEALTH_NAME:
        result["engine"] = engine
    return result


async def _reclaim_stale_targets(conn) -> None:
    await conn.execute(
        """
        UPDATE recon_targets
        SET status = 'pending',
            error = 'stale SpiderFoot run reclaimed',
            updated_at = NOW()
        WHERE status = 'in_progress'
          AND updated_at < NOW() - ($1::int * INTERVAL '1 minute')
        """,
        stale_target_minutes(),
    )


def normalize_observation(target_id: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    module = raw.get("module") or raw.get("moduleName") or raw.get("source")
    observation_type = raw.get("type") or raw.get("eventType") or raw.get("dataType")
    value = raw.get("value") or raw.get("data") or raw.get("content")
    if not module or not observation_type or value in {None, ""}:
        return None
    confidence = raw.get("confidence", 0.25)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.25
    return {
        "target_id": target_id,
        "module": str(module),
        "observation_type": str(observation_type),
        "value": str(value),
        "confidence": max(0.0, min(confidence, 1.0)),
        "raw_json": raw,
    }


async def _claim_target(conn, policy: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if policy is None:
        policy, _state, reason = admission_snapshot()
        if reason:
            raise AdmissionDenied(reason)
    row = await conn.fetchrow(
        """
        SELECT id::text, target_type, target_value, source, priority, scope_json
        FROM recon_targets
        WHERE status = 'pending'
          AND target_type = ANY($1::text[])
          AND target_type = ANY($2::text[])
          AND source = ANY($3::text[])
          AND (
              COALESCE(scope_json->>'account_identity', scope_json->>'account_id',
                       scope_json->>'owner_account_id') IS NULL
              OR COALESCE(scope_json->>'account_identity', scope_json->>'account_id',
                          scope_json->>'owner_account_id') = ANY($4::text[])
          )
        ORDER BY priority, created_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
        """,
        sorted(SUPPORTED_TYPES),
        policy["approved_target_types"],
        policy["approved_source_identities"],
        policy["approved_account_identities"],
    )
    if not row:
        return None
    candidate = dict(row)
    scope = _json_dict(candidate.get("scope_json"))
    account_identity = (
        scope.get("account_identity") or scope.get("account_id") or scope.get("owner_account_id")
    )
    token = reserve_claim(
        source_identity=str(candidate["source"]),
        target_type=str(candidate["target_type"]),
        account_identity=str(account_identity) if account_identity is not None else None,
    )
    try:
        state = admission_snapshot(policy)[1]
        claimed = await conn.fetchrow(
            """
            UPDATE recon_targets
            SET status = 'in_progress', error = NULL, updated_at = NOW(),
                scope_json = scope_json || jsonb_build_object(
                    'admission_policy_id', $2::text,
                    'admission_cohort_limit', $3::int
                )
            WHERE id = $1::uuid AND status = 'pending'
            RETURNING id::text, target_type, target_value, source, priority, scope_json
            """,
            candidate["id"],
            policy["policy_id"],
            state["cohort_limit"],
        )
        if not claimed:
            release_claim(token)
            return None
        result = dict(claimed)
        identity_hash = hashlib.sha256(
            f"{result['target_type']}:{result['target_value']}".encode("utf-8")
        ).hexdigest()[:16]
        commit_claim(token, target_identity=identity_hash)
        return result
    except BaseException:
        release_claim(token)
        raise


async def _peek_pending_target(conn, policy: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Read a candidate without locks or state changes for dry-run previews."""
    if policy is None:
        policy, _state, reason = admission_snapshot()
        if reason:
            raise AdmissionDenied(reason)
    row = await conn.fetchrow(
        """
        SELECT id::text, target_type, target_value, source, priority, scope_json
        FROM recon_targets
        WHERE status = 'pending'
          AND target_type = ANY($1::text[])
          AND target_type = ANY($2::text[])
          AND source = ANY($3::text[])
          AND (
              COALESCE(scope_json->>'account_identity', scope_json->>'account_id',
                       scope_json->>'owner_account_id') IS NULL
              OR COALESCE(scope_json->>'account_identity', scope_json->>'account_id',
                          scope_json->>'owner_account_id') = ANY($4::text[])
          )
        ORDER BY priority, created_at
        LIMIT 1
        """,
        sorted(SUPPORTED_TYPES),
        policy["approved_target_types"],
        policy["approved_source_identities"],
        policy["approved_account_identities"],
    )
    return dict(row) if row else None


async def _store_observations(conn, observations: list[dict[str, Any]]) -> int:
    if not observations:
        return 0
    await conn.executemany(
        """
        INSERT INTO recon_observations (
            target_id, module, observation_type, value, value_hash, confidence, raw_json,
            first_seen_at, last_seen_at
        )
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb, NOW(), NOW())
        ON CONFLICT (target_id, module, observation_type, value_hash) DO UPDATE SET
            confidence = GREATEST(recon_observations.confidence, EXCLUDED.confidence),
            value = EXCLUDED.value,
            raw_json = EXCLUDED.raw_json,
            last_seen_at = NOW()
        """,
        [
            (
                row["target_id"],
                row["module"],
                row["observation_type"],
                row["value"],
                hashlib.sha256(str(row["value"]).encode("utf-8")).hexdigest(),
                row["confidence"],
                json.dumps(row["raw_json"]),
            )
            for row in observations
        ],
    )
    return len(observations)



# ---------------------------------------------------------------------------
# GHunt: email -> Google account enrichment
# ---------------------------------------------------------------------------
# Runs in its own ISOLATED venv (/opt/ghunt-venv, baked in Dockerfile.spiderfoot).
# The recon worker shells out via src.core.ghunt_enrich.run_lookup, which honours
# GHUNT_BIN + GHUNT_CREDS. This handler:
#   * gates on ghunt_enrich.is_configured() (no-op if creds absent),
#   * rate-limits to concurrency 1 with jitter (Google anti-abuse is real),
#   * normalizes the ghunt JSON into RECON observations only — no strong
#     identity_signals here; a Google display name is weak evidence and the
#     analyzer bridge decides linking policy separately.
_GHUNT_RATE_LOCK = asyncio.Lock()


def _ghunt_flatten_profile(email: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn one ghunt JSON dump into a small list of observation dicts.

    Actual ghunt 2.3.4 --json shape (verified against a real lookup):
        {
          "PROFILE_CONTAINER": {
            "profile": {
              "personId": "<gaia>",
              "names":    {"PROFILE": {"fullname": "...", ...}, ...},
              "emails":   {"ACCOUNT|CONTACT|PROFILE": {"value": "..."}},
              "profilePhotos": {"PROFILE": {"url": "...", "isDefault": bool}},
              "coverPhotos":   {"PROFILE": {...}},
              ...
            },
            "maps": {...}, "calendar": {...}, "play_games": {...}
          }
        }

    Conservative: one primary GOOGLE_ACCOUNT row (raw_json = full dump) plus
    weak side-observations (gaia id, human name, non-default avatar) when
    present. Downstream identity linking is left to the analyzer bridge; a
    Google display name is not strong evidence on its own."""
    email = (email or "").strip().lower()
    if not email:
        return []
    container = data.get("PROFILE_CONTAINER") if isinstance(data.get("PROFILE_CONTAINER"), dict) else data
    container = container if isinstance(container, dict) else {}
    profile = container.get("profile") if isinstance(container.get("profile"), dict) else {}

    gaia_id: str | None = None
    if isinstance(profile.get("personId"), (str, int)):
        gaia_id = str(profile["personId"]).strip() or None

    display_name: str | None = None
    names = profile.get("names")
    # names is dict keyed by container ("PROFILE", ...) with {fullname, firstName, lastName}.
    if isinstance(names, dict):
        preferred = names.get("PROFILE") if isinstance(names.get("PROFILE"), dict) else None
        candidate_dicts = [preferred] if preferred else [v for v in names.values() if isinstance(v, dict)]
        for cand in candidate_dicts:
            for key in ("fullname", "firstName", "first_name", "lastName"):
                val = cand.get(key)
                if isinstance(val, str) and val.strip():
                    display_name = val.strip()
                    break
            if display_name:
                break
    elif isinstance(names, list) and names:
        first = names[0] if isinstance(names[0], dict) else {}
        display_name = first.get("fullname") or first.get("firstName") or first.get("first_name")

    photo_url: str | None = None
    photo_is_default: bool | None = None
    photos = profile.get("profilePhotos")
    # profilePhotos is dict keyed by container; each entry has {url, isDefault, ...}.
    if isinstance(photos, dict):
        preferred = photos.get("PROFILE") if isinstance(photos.get("PROFILE"), dict) else None
        candidate_dicts = [preferred] if preferred else [v for v in photos.values() if isinstance(v, dict)]
        for cand in candidate_dicts:
            url = cand.get("url")
            if isinstance(url, str) and url.strip():
                photo_url = url
                photo_is_default = bool(cand.get("isDefault"))
                break
    elif isinstance(photos, list) and photos:
        first = photos[0] if isinstance(photos[0], dict) else {}
        if isinstance(first.get("url"), str):
            photo_url = first["url"]
            photo_is_default = bool(first.get("isDefault"))

    rows: list[dict[str, Any]] = []
    # Primary row: this email is a real Google account. Confidence is moderate;
    # the analyzer decides whether to bind it to a stronger identity.
    rows.append({
        "module": GHUNT_MODULE,
        "observation_type": GHUNT_OBSERVATION_TYPE,
        "value": email,
        "confidence": 0.6,
        "raw_json": data,
    })
    if gaia_id:
        rows.append({
            "module": GHUNT_MODULE,
            "observation_type": "GAIA_ID",
            "value": gaia_id,
            "confidence": 0.6,
            "raw_json": {"email": email, "gaia_id": gaia_id},
        })
    if display_name:
        rows.append({
            "module": GHUNT_MODULE,
            "observation_type": "HUMAN_NAME",
            "value": display_name,
            # Weak: Google display names are attacker-controlled.
            "confidence": 0.3,
            "raw_json": {"email": email, "display_name": display_name},
        })
    # Only emit the avatar observation when it's NOT the default silhouette.
    # A default avatar carries no identifying signal.
    if photo_url and photo_is_default is False:
        rows.append({
            "module": GHUNT_MODULE,
            "observation_type": "AVATAR",
            "value": photo_url,
            "confidence": 0.4,
            "raw_json": {"email": email, "photo_url": photo_url, "is_default": photo_is_default},
        })
    return rows


async def _run_ghunt_cli(
    target: dict[str, Any],
    *,
    worker_label: int | str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run ghunt against ONE email target. Returns (observations, meta).

    Serialized via a process-wide asyncio.Lock (concurrency=1) with a small
    jittered sleep between calls so we don't dogpile Google's unofficial
    endpoints. On any error / skip, returns ([], meta) so the caller can
    still update recon_targets.status without crashing the worker loop.
    """
    # Imported lazily so the module still imports when ghunt_enrich is absent.
    from src.core import ghunt_enrich  # type: ignore

    ready, reason = ghunt_enrich.is_configured()
    if not ready:
        return [], {"status": "skipped", "reason": reason}

    email = str(target.get("target_value") or "").strip().lower()
    if not email or "@" not in email:
        return [], {"status": "error", "reason": f"not an email: {email!r}"}

    try:
        timeout = min(
            90.0, max(1.0, float(os.getenv("GHUNT_TARGET_TIMEOUT_SECONDS", "90")))
        )
    except ValueError:
        timeout = 90.0
    try:
        min_delay = min(60.0, max(8.0, float(os.getenv("GHUNT_MIN_DELAY_SECONDS", "8"))))
    except ValueError:
        min_delay = 8.0
    try:
        max_jitter = min(6.0, max(0.0, float(os.getenv("GHUNT_MAX_JITTER_SECONDS", "6"))))
    except ValueError:
        max_jitter = 6.0
    async with _GHUNT_RATE_LOCK:
        # Jittered pre-call delay so consecutive lookups don't hit Google in a
        # tight loop. Bounded so a full pilot of ~10 emails takes ~2 minutes.
        await asyncio.sleep(min_delay + random.uniform(0.0, max(0.0, max_jitter)))
        result = await ghunt_enrich.run_lookup(email, timeout=timeout)
    status = str(result.get("status") or "error")
    if status != "ok":
        return [], {"status": status, "reason": str(result.get("reason") or "")}
    data = result.get("data")
    if not isinstance(data, dict):
        return [], {"status": "error", "reason": "ghunt returned non-dict JSON"}
    rows = _ghunt_flatten_profile(email, data)
    for row in rows:
        row["target_id"] = target["id"]
    return rows, {"status": "ok", "rows": len(rows)}

def _maigret_selected(target_type: str, modules: list[str]) -> bool:
    """Return True when this target should run through maigret, not SpiderFoot."""
    if (target_type or "").lower() != "username":
        return False
    if modules == [MAIGRET_MODULE]:
        return True
    if MAIGRET_MODULE in modules and len(modules) == 1:
        return True
    engine = os.getenv("RECON_USERNAME_ENGINE", "").strip().lower()
    return engine == "maigret"

def _ghunt_selected(target_type: str, scope_modules: list[str] | None) -> bool:
    """Return True when this target should run through ghunt.

    Explicit opt-in only: target must be an email AND either
      (a) scope.modules contains 'ghunt' as the sole engine, OR
      (b) RECON_EMAIL_ENGINE env is 'ghunt'.
    Never runs on non-email targets — ghunt only understands Google accounts."""
    if (target_type or "").lower() != "email":
        return False
    mods = [m.strip().lower() for m in (scope_modules or []) if isinstance(m, str)]
    if mods and GHUNT_MODULE in mods and all(m == GHUNT_MODULE for m in mods):
        return True
    engine = os.getenv("RECON_EMAIL_ENGINE", "").strip().lower()
    return engine == GHUNT_MODULE



def _generate_control_username() -> str:
    """Random 16-char alphanumeric username used to detect universal-200 sites.

    Any site that reports "Claimed" for this random handle is a false-positive
    site that returns 200/success for any input — we exclude those from the
    real target's observations (same technique SpiderFoot's sfp_accounts uses).
    """
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _claimed_sitenames(rows: list[dict[str, Any]]) -> set[str]:
    """Return lowercase site names maigret reported as ``status: Claimed``.

    maigret 0.5.x nests the verdict under an inner ``status`` dict:
        {"sitename": "GitHub", "status": {"status": "Claimed", "site_name": ...}}
    Older formats put it at the top level. Handle both defensively.
    """
    names: set[str] = set()
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        status_field = entry.get("status")
        if isinstance(status_field, dict):
            status_str = str(status_field.get("status") or "").strip().lower()
            inner_name = status_field.get("site_name")
        else:
            status_str = str(status_field or "").strip().lower()
            inner_name = None
        if "claimed" not in status_str or "not" in status_str:
            continue
        name = entry.get("sitename") or entry.get("site_name") or inner_name
        if name:
            names.add(str(name).strip().lower())
    return names


def _maigret_rows_to_observations(
    target_id: str,
    rows: list[dict[str, Any]],
    *,
    exclude_sitenames: set[str] | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Normalize maigret site entries into recon_observations rows.

    Returns ``(observations, blocked_sitenames)``. Only entries with a Claimed
    status and a real profile URL become observations. Any sitename appearing
    in ``exclude_sitenames`` (the cached universal-200 FP blocklist — xvideos,
    roblox, op.gg, twitchtracker, ...) is dropped, and the site name is added
    to ``blocked_sitenames`` so the caller can update ``hit_count`` for the
    ``recon_maigret_fp_sites`` audit trail.

    Per-row confidence is a HTTP-only heuristic: top-500 site rank -> 0.7,
    else 0.5. The analyzer bridge caps cross_platform_link at 0.5 downstream.
    """
    exclude = {
        str(name).strip().lower()
        for name in (exclude_sitenames or set())
        if name
    }
    observations: list[dict[str, Any]] = []
    blocked: set[str] = set()
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        status_field = entry.get("status")
        if isinstance(status_field, dict):
            status_str = str(status_field.get("status") or "").strip().lower()
            inner_name = status_field.get("site_name")
        else:
            status_str = str(status_field or "").strip().lower()
            inner_name = None
        # Skip everything except an explicit Claimed hit. maigret emits
        # "claimed", "not claimed", "unknown", "illegal", "available" — only
        # "claimed" is evidence of an actual account.
        if "claimed" not in status_str or "not" in status_str:
            continue
        sitename = entry.get("sitename") or entry.get("site_name") or inner_name
        sitename_lc = str(sitename).strip().lower() if sitename else ""
        if sitename_lc and sitename_lc in exclude:
            # Universal-200 false-positive site — recorded for hit tracking.
            blocked.add(sitename_lc)
            continue
        url = (
            entry.get("url_user")
            or entry.get("url")
            or entry.get("profile_url")
            or entry.get("link")
        )
        if not url:
            continue
        rank_val = entry.get("rank") or entry.get("alexa_rank") or 0
        try:
            rank = int(rank_val)
        except (TypeError, ValueError):
            rank = 0
        confidence = 0.7 if 0 < rank <= 500 else 0.5
        observations.append(
            {
                "target_id": target_id,
                "module": MAIGRET_MODULE,
                "observation_type": "ACCOUNT_EXTERNAL_OWNED",
                "value": str(url),
                "confidence": confidence,
                "raw_json": entry,
            }
        )
    return observations, blocked


def _parse_maigret_report_dir(report_dir: Path) -> list[dict[str, Any]]:
    """Read the maigret output folder and return raw site entries."""
    rows: list[dict[str, Any]] = []
    # maigret 0.5.x writes `report_<username>_ndjson.json` when invoked with
    # `-J ndjson -fo <dir>` (one JSON object per line). Older builds used
    # `report_<username>.ndjson` — accept both to stay forward/backward safe.
    ndjson_paths = sorted(report_dir.glob("report_*_ndjson.json")) + sorted(
        report_dir.glob("report_*.ndjson")
    )
    for path in ndjson_paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    if rows:
        return rows
    # Fallback: simple JSON summary (dict keyed by sitename)
    for path in sorted(report_dir.glob("report_*_simple.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            for site_name, entry in payload.items():
                if isinstance(entry, dict):
                    entry.setdefault("sitename", site_name)
                    rows.append(entry)
        elif isinstance(payload, list):
            rows.extend(item for item in payload if isinstance(item, dict))
    return rows


async def _run_maigret_subprocess(
    username: str,
    timeout_seconds: int,
    *,
    worker_label: int | str | None = None,
    keep_dir: str | None = None,
) -> list[dict[str, Any]]:
    """Run maigret ONCE against a username; return raw ndjson site entries.

    HTTP-only by design: no --cloudflare-bypass, no browser. Bounded via the
    MAIGRET_TOP_SITES / MAIGRET_NUM_REQUESTS / MAIGRET_HTTP_TIMEOUT_SECONDS env.
    On per-target timeout, SIGKILL the whole process group and raise.
    """
    executable = os.getenv("MAIGRET_CLI", "maigret")
    if shutil.which(executable) is None and not os.path.exists(executable):
        raise RuntimeError(f"MAIGRET_CLI executable not found: {executable}")
    try:
        top_sites = min(100, max(1, int(os.getenv("MAIGRET_TOP_SITES", "100"))))
    except ValueError:
        top_sites = 100
    try:
        num_requests = min(10, max(1, int(os.getenv("MAIGRET_NUM_REQUESTS", "10"))))
    except ValueError:
        num_requests = 10
    http_timeout = str(_bounded_timeout_seconds(
        "MAIGRET_HTTP_TIMEOUT_SECONDS", 10, 30,
    ))

    def retain_reports(source: Path) -> None:
        if not keep_dir:
            return
        dst_root = Path(keep_dir)
        evidence = ensure_evidence_capacity()
        if not dst_root.resolve().is_relative_to(evidence.resolve()):
            raise RuntimeError("MAIGRET_REPORT_KEEPDIR must be inside RECON_EVIDENCE_ROOT")
        dst_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        dst_root.chmod(0o700)
        for path in source.glob("report_*"):
            if not path.is_file():
                continue
            ensure_evidence_capacity(additional_bytes=path.stat().st_size)
            destination = dst_root / f"{secrets.token_hex(8)}-{path.name}"
            shutil.copy2(path, destination)
            destination.chmod(0o600)

    with tempfile.TemporaryDirectory(prefix="maigret-") as tmpdir:
        cmd = [
            executable,
            username,
            "--top-sites", str(top_sites),
            "--timeout", str(http_timeout),
            "-n", str(num_requests),
            "--no-progressbar",
            "-J", "ndjson",
            "-fo", tmpdir,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_spiderfoot_env(worker_label),
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (AttributeError, ProcessLookupError):
                proc.kill()
            await proc.communicate()
            retain_reports(Path(tmpdir))
            raise RuntimeError(
                f"maigret timed out after {timeout_seconds}s for {username}"
            )
        # maigret exits non-zero when zero sites match; we still parse the
        # report dir. Only a missing report dir is a real failure.
        raw_rows = _parse_maigret_report_dir(Path(tmpdir))
        retain_reports(Path(tmpdir))
        if not raw_rows and proc.returncode not in (0, 1):
            err_text = (stderr or stdout).decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"maigret failed for {username} "
                f"(exit={proc.returncode}): {err_text}"
            )
    return raw_rows


# ---------------------------------------------------------------------------
# maigret false-positive-site cached blocklist
# ---------------------------------------------------------------------------
# The previous revision ran a random 16-char control username IN PARALLEL
# with every real target so it could subtract universal-200 sites (xvideos,
# roblox, op.gg, ...) from the target's Claimed hits. That cut FPs from
# ~76% to ~10%, but doubled maigret subprocess count for every backfill
# target AND still let intermittent-200 sites slip through when a random
# control happened to miss them.
#
# Upgrade: build the FP-site set ONCE from N=5 random controls, persist it in
# `recon_maigret_fp_sites` with a TTL, and per REAL target run maigret
# EXACTLY ONCE, dropping any Claimed site whose sitename is in the cached
# blocklist. Backfill stays at 1 real run/target; precision reflects the
# multi-control union. Refresh is a manual/cron operation:
#     docker exec unifiedcollector_spiderfoot \
#         python -m src.recon_maigret_fp_refresh --force --controls 5
_MAIGRET_FP_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS recon_maigret_fp_sites (
    sitename          TEXT PRIMARY KEY,
    sample_url        TEXT,
    sample_control    TEXT,
    controls_seen     INTEGER NOT NULL DEFAULT 1,
    hit_count         BIGINT  NOT NULL DEFAULT 0,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_recon_maigret_fp_last_refreshed
    ON recon_maigret_fp_sites (last_refreshed_at DESC);
"""

# Random 64-bit constant; every worker uses the same key so concurrent
# refresh attempts serialize on this pg_advisory_lock slot.
MAIGRET_FP_REFRESH_LOCK_KEY = -8834912738475629487
DEFAULT_MAIGRET_FP_TTL_DAYS = "7"
DEFAULT_MAIGRET_FP_NUM_CONTROLS = "5"

_maigret_fp_table_ready = False


async def _ensure_maigret_fp_table(conn) -> None:
    """Idempotent CREATE TABLE for the FP-site cache. No-op after first call."""
    global _maigret_fp_table_ready
    if _maigret_fp_table_ready:
        return
    await conn.execute(_MAIGRET_FP_TABLE_DDL)
    _maigret_fp_table_ready = True


async def load_maigret_fp_blocklist(conn) -> set[str]:
    """Return the cached set of universal-200 FP sitenames (lowercase)."""
    await _ensure_maigret_fp_table(conn)
    rows = await conn.fetch("SELECT sitename FROM recon_maigret_fp_sites")
    return {row["sitename"] for row in rows}


async def bump_maigret_fp_hits(conn, sitenames: set[str]) -> None:
    """Increment ``hit_count`` for each blocklisted sitename we filtered."""
    if not sitenames:
        return
    await _ensure_maigret_fp_table(conn)
    await conn.execute(
        """
        UPDATE recon_maigret_fp_sites
           SET hit_count = hit_count + 1
         WHERE sitename = ANY($1::text[])
        """,
        list(sitenames),
    )


async def refresh_maigret_fp_blocklist(
    conn,
    *,
    num_controls: int = 5,
    worker_label: int | str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Refresh the cached FP-site blocklist by running maigret on N controls.

    A site claimed by ANY of the N random controls is a universal-200 FP.
    Serialized via ``pg_advisory_lock`` so concurrent refresh attempts are
    safe. ``force=True`` truncates the existing table before repopulating (used
    by the manual bootstrap CLI). Control runs are SEQUENTIAL to keep RAM
    bounded to a single maigret process at a time.
    """
    try:
        policy, _state, block = admission_snapshot()
    except AdmissionPolicyError as exc:
        return {"status": "admission_unavailable", "controls_run": 0, "controls_failed": 0, "sites": 0, "error": str(exc)}
    sibling_policy = policy["sibling_work"].get("maigret_fp_refresh")
    if block or not sibling_policy or not sibling_policy.get("enabled"):
        return {
            "status": "admission_halted" if block else "sibling_disabled",
            "controls_run": 0,
            "controls_failed": 0,
            "sites": 0,
            "error": block,
        }
    sibling_remaining = int(sibling_policy["max_claims"]) - int(
        _state.get("sibling_claims", {}).get("maigret_fp_refresh", 0)
    )
    total_remaining = min(
        int(_state["cohort_limit"]), int(policy["limits"]["max_claims"]),
    ) - int(_state["claims_used"])
    try:
        requested_controls = max(1, int(num_controls))
    except (TypeError, ValueError):
        requested_controls = 1
    num_controls = min(requested_controls, sibling_remaining, total_remaining)
    if num_controls <= 0:
        return {"status": "admission_halted", "controls_run": 0, "controls_failed": 0, "sites": 0, "error": "sibling or cohort allowance exhausted"}
    lock_key = MAIGRET_FP_REFRESH_LOCK_KEY
    if force:
        await conn.execute("SELECT pg_advisory_lock($1::bigint)", lock_key)
        got_lock = True
    else:
        got_lock = bool(
            await conn.fetchval("SELECT pg_try_advisory_lock($1::bigint)", lock_key)
        )
    if not got_lock:
        return {"status": "skipped_locked", "controls_run": 0, "sites": 0}
    first_token: str | None = None
    try:
        try:
            first_token = reserve_claim(
                source_identity="sibling:maigret_fp_refresh",
                target_type="username",
                sibling_identity="maigret_fp_refresh",
            )
        except AdmissionDenied as exc:
            return {"status": "admission_halted", "controls_run": 0, "controls_failed": 0, "sites": 0, "error": str(exc)}
        await _ensure_maigret_fp_table(conn)
        if force:
            await conn.execute("TRUNCATE TABLE recon_maigret_fp_sites")
        timeout = _bounded_timeout_seconds(
            "MAIGRET_TARGET_TIMEOUT_SECONDS",
            int(DEFAULT_MAIGRET_TARGET_TIMEOUT_SECONDS),
            600,
        )
        aggregate: dict[str, dict[str, Any]] = {}
        controls_run = 0
        controls_failed = 0
        admission_status = "refreshed"
        admission_error = None
        for index in range(num_controls):
            if index == 0:
                admission_token = first_token
            else:
                try:
                    admission_token = reserve_claim(
                        source_identity="sibling:maigret_fp_refresh",
                        target_type="username",
                        sibling_identity="maigret_fp_refresh",
                    )
                except AdmissionDenied as exc:
                    admission_status = "admission_halted"
                    admission_error = str(exc)
                    break
            control = _generate_control_username()
            try:
                rows = await _run_maigret_subprocess(
                    control, timeout, worker_label=worker_label
                )
            except RuntimeError:
                commit_claim(admission_token)
                controls_failed += 1
                continue
            except BaseException:
                commit_claim(admission_token)
                raise
            commit_claim(admission_token)
            controls_run += 1
            for entry in rows:
                if not isinstance(entry, dict):
                    continue
                status_field = entry.get("status")
                if isinstance(status_field, dict):
                    status_str = str(status_field.get("status") or "").strip().lower()
                    inner_name = status_field.get("site_name")
                else:
                    status_str = str(status_field or "").strip().lower()
                    inner_name = None
                if "claimed" not in status_str or "not" in status_str:
                    continue
                name = entry.get("sitename") or entry.get("site_name") or inner_name
                if not name:
                    continue
                key = str(name).strip().lower()
                url = (
                    entry.get("url_user")
                    or entry.get("url")
                    or entry.get("profile_url")
                    or ""
                )
                bucket = aggregate.setdefault(
                    key,
                    {
                        "sample_url": str(url) or None,
                        "sample_control": control,
                        "controls": 0,
                    },
                )
                bucket["controls"] += 1
        if aggregate:
            await conn.executemany(
                """
                INSERT INTO recon_maigret_fp_sites
                    (sitename, sample_url, sample_control, controls_seen, last_refreshed_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (sitename) DO UPDATE SET
                    controls_seen     = GREATEST(
                        recon_maigret_fp_sites.controls_seen, EXCLUDED.controls_seen
                    ),
                    sample_url        = COALESCE(
                        recon_maigret_fp_sites.sample_url, EXCLUDED.sample_url
                    ),
                    sample_control    = COALESCE(
                        recon_maigret_fp_sites.sample_control, EXCLUDED.sample_control
                    ),
                    last_refreshed_at = NOW()
                """,
                [
                    (key, info["sample_url"], info["sample_control"], info["controls"])
                    for key, info in aggregate.items()
                ],
            )
        return {
            "status": admission_status,
            "controls_requested": num_controls,
            "controls_run": controls_run,
            "controls_failed": controls_failed,
            "sites": len(aggregate),
            "forced": force,
            "error": admission_error,
        }
    finally:
        if first_token:
            release_claim(first_token)
        await conn.execute("SELECT pg_advisory_unlock($1::bigint)", lock_key)


async def _run_maigret_cli(
    target: dict[str, Any],
    timeout_seconds: int,
    *,
    worker_label: int | str | None = None,
    fp_blocklist: set[str] | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Run maigret HTTP-only against a target, filter via the cached FP blocklist.

    Returns ``(observations, blocked_sitenames)`` so the caller can
    ``bump_maigret_fp_hits`` for the blocklisted sites we just filtered.
    Backfill-friendly: exactly ONE maigret subprocess per target. The
    per-target control from the previous revision is REMOVED — the cached
    multi-control blocklist supersedes it.
    """
    root = ensure_evidence_capacity()
    keep_dir = (os.getenv("MAIGRET_REPORT_KEEPDIR", "") or "").strip()
    keep_path = Path(keep_dir) if keep_dir else root / "maigret"
    if not keep_path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("MAIGRET_REPORT_KEEPDIR must be inside RECON_EVIDENCE_ROOT")
    keep_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    keep_path.chmod(0o700)
    target_rows = await _run_maigret_subprocess(
        str(target["target_value"]),
        timeout_seconds,
        worker_label=worker_label,
        keep_dir=str(keep_path),
    )
    return _maigret_rows_to_observations(
        str(target["id"]),
        target_rows,
        exclude_sitenames=fp_blocklist or set(),
    )



async def _run_spiderfoot_cli(
    target: dict[str, Any],
    modules: list[str],
    timeout_seconds: int,
    *,
    worker_label: int | str | None = None,
) -> list[dict[str, Any]]:
    cli = os.getenv("SPIDERFOOT_CLI")
    if not cli:
        raise RuntimeError("SPIDERFOOT_CLI is not configured")
    command = shlex.split(cli)
    if not command:
        raise RuntimeError("SPIDERFOOT_CLI is not configured")
    executable = command[0]
    if shutil.which(executable) is None and not os.path.exists(executable):
        raise RuntimeError(f"SPIDERFOOT_CLI executable not found: {executable}")
    ensure_evidence_capacity()
    proc = await asyncio.create_subprocess_exec(
        *command,
        "-s",
        target["target_value"],
        "-m",
        ",".join(modules),
        "-o",
        "json",
        "-max-threads",
        str(spiderfoot_max_threads()),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_spiderfoot_env(worker_label),
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError):
            proc.kill()
        partial_stdout, partial_stderr = await proc.communicate()
        store_private_evidence("spiderfoot", ".timeout.stdout.json", partial_stdout)
        if partial_stderr:
            store_private_evidence("spiderfoot", ".timeout.stderr.log", partial_stderr)
        raise RuntimeError(f"SpiderFoot timed out after {timeout_seconds}s")
    store_private_evidence("spiderfoot", ".stdout.json", stdout)
    if stderr:
        store_private_evidence("spiderfoot", ".stderr.log", stderr)
    if proc.returncode != 0:
        raise RuntimeError((stderr or stdout).decode("utf-8", errors="replace")[:500])
    return parse_spiderfoot_stdout(stdout)


def normalize_spiderfoot_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("events") or []
        return [item for item in data if isinstance(item, dict)]
    return []


async def run_spiderfoot_once(
    conn,
    *,
    dry_run: bool = False,
    worker_label: int | str | None = None,
) -> dict[str, Any]:
    # Scope admission is intentionally open. The shared network namespace's
    # nftables output chain is the non-bypassable safety boundary; fail closed
    # before claiming any work if that kernel policy is absent.
    if not network_policy_installed():
        error = "public-only recon egress policy is not installed"
        if not dry_run:
            await _mark_source_health(conn, "degraded", error)
        return {"status": "guard_unavailable", "target": None, "observations": 0, "error": error, "dry_run": dry_run}

    try:
        policy, _admission_state, admission_block = admission_snapshot()
    except AdmissionPolicyError as exc:
        error = str(exc)
        if not dry_run:
            await _mark_source_health(conn, "degraded", error)
        return {"status": "admission_unavailable", "target": None, "observations": 0, "error": error, "dry_run": dry_run}
    configured_modules = [
        item.strip()
        for item in (os.getenv("SPIDERFOOT_MODULES") or ",".join(DEFAULT_MODULES)).split(",")
        if item.strip()
    ]
    try:
        validate_zero_spend_modules(configured_modules, policy=policy)
    except AdmissionDenied as exc:
        halt_admission("zero_spend_violation: " + str(exc))
        return {"status": "zero_spend_rejected", "target": None, "observations": 0, "error": str(exc), "dry_run": dry_run}
    if admission_block:
        if not dry_run:
            # A policy halt intentionally prevents claims; the living worker is
            # still healthy and must heartbeat so Docker does not restart it.
            await _mark_source_health(conn, "running", None, success=True)
        return {
            "status": "admission_halted",
            "target": None,
            "observations": 0,
            "error": admission_block,
            "dry_run": dry_run,
        }

    if dry_run:
        try:
            target = await _peek_pending_target(conn, policy)
        except (AdmissionDenied, AdmissionPolicyError) as exc:
            return {"status": "admission_halted", "target": None, "observations": 0, "error": str(exc), "dry_run": True}
    else:
        try:
            async with conn.transaction():
                # Reclaiming stale rows is a write, so the installed admission
                # policy must be active before this recovery mutation as well.
                await _reclaim_stale_targets(conn)
                target = await _claim_target(conn, policy)
        except AdmissionDenied as exc:
            return {"status": "admission_halted", "target": None, "observations": 0, "error": str(exc), "dry_run": False}
        except AdmissionPolicyError as exc:
            error = str(exc)
            await _mark_source_health(conn, "degraded", error)
            return {"status": "admission_unavailable", "target": None, "observations": 0, "error": error, "dry_run": False}
    if not target:
        if not dry_run:
            await _mark_source_health(conn, "running", None, success=True)
        return {"status": "dry_run" if dry_run else "idle", "target": None, "observations": 0, "dry_run": dry_run}
    scope = _json_dict(target.get("scope_json"))
    try:
        await asyncio.to_thread(
            validate_recon_target,
            str(target.get("target_type") or ""),
            str(target.get("target_value") or ""),
        )
    except UnsafeReconTarget as exc:
        reason = str(exc)[:500]
        if dry_run:
            return {"status": "dry_run", "target": target, "observations": 0, "rejected": reason, "dry_run": True}
        await conn.execute(
            "UPDATE recon_targets SET status = 'blocked', error = $2, updated_at = NOW() WHERE id = $1::uuid",
            target["id"],
            reason,
        )
        await _mark_source_health(conn, "running", None, success=True)
        return {"status": "blocked", "target": target, "observations": 0, "error": reason, "dry_run": dry_run}

    # GHunt: intercept email targets tagged with scope.modules=['ghunt'] (or when
    # RECON_EMAIL_ENGINE=ghunt) BEFORE SpiderFoot module resolution. GHunt does not
    # use sfp_* modules, so passing it through allowed_modules() would either return
    # DEFAULT_MODULES (wrong — SpiderFoot would then run sfp_dnsresolve on an email)
    # or an empty list (target incorrectly blocked). Handle it here as a first-class
    # engine, same pattern as maigret but placed earlier because ghunt scope.modules
    # is never a subset of the SpiderFoot module set.
    scope_modules_raw = scope.get("modules")
    scope_modules_list = scope_modules_raw if isinstance(scope_modules_raw, list) else []
    if _ghunt_selected(target["target_type"], scope_modules_list):
        if dry_run:
            return {"status": "dry_run", "target": target, "engine": GHUNT_MODULE, "observations": 0, "dry_run": True}
        try:
            rows, meta = await _run_ghunt_cli(target, worker_label=worker_label)
            status = str(meta.get("status") or "error")
            if status == "skipped":
                # GHunt is disabled (no creds, no bin). Return the target to pending
                # so a later run can retry when creds are mounted; do NOT mark failed.
                await conn.execute(
                    "UPDATE recon_targets SET status = 'pending', error = $2, updated_at = NOW() WHERE id = $1::uuid",
                    target["id"],
                    f"ghunt skipped: {meta.get('reason','')[:200]}",
                )
                await _mark_source_health(conn, "running", None, success=True)
                return {"status": "skipped", "target": target, "engine": GHUNT_MODULE, "observations": 0, "reason": meta.get("reason"), "dry_run": False}
            if status != "ok":
                reason = str(meta.get("reason") or "")[:500]
                await conn.execute(
                    "UPDATE recon_targets SET status = 'failed', error = $2, updated_at = NOW() WHERE id = $1::uuid",
                    target["id"],
                    reason,
                )
                # ghunt error (e.g. account not found, creds rotated) is target-level.
                await _mark_source_health(conn, "running", None, success=True)
                return {"status": "failed", "target": target, "engine": GHUNT_MODULE, "observations": 0, "error": reason, "dry_run": False}
            written = await _store_observations(conn, rows)
            if written == 0:
                return await _fail_empty_target(conn, target, GHUNT_MODULE)
            await conn.execute(
                "UPDATE recon_targets SET status = 'completed', error = NULL, updated_at = NOW() WHERE id = $1::uuid",
                target["id"],
            )
            await _mark_source_health(conn, "running", None, success=True)
            return {"status": "completed", "target": target, "engine": GHUNT_MODULE, "observations": written, "dry_run": False}
        except Exception as exc:  # noqa: BLE001
            error = str(exc)[:500]
            await conn.execute(
                "UPDATE recon_targets SET status = 'failed', error = $2, updated_at = NOW() WHERE id = $1::uuid",
                target["id"],
                error,
            )
            await _mark_source_health(conn, "degraded", error)
            return {"status": "failed", "target": target, "engine": GHUNT_MODULE, "observations": 0, "error": error, "dry_run": False}


    try:
        modules = effective_modules_for_target(scope.get("modules"))
    except ValueError as exc:
        error = str(exc)[:500]
        requested = scope.get("modules")
        requested_list = requested if isinstance(requested, list) else str(requested or "").split(",")
        try:
            validate_zero_spend_modules([str(item).strip() for item in requested_list], policy=policy)
        except AdmissionDenied as paid_exc:
            halt_admission("zero_spend_violation: " + str(paid_exc))
            if not dry_run:
                await conn.execute(
                    "UPDATE recon_targets SET status='failed', error=$2, updated_at=NOW() WHERE id=$1::uuid",
                    target["id"],
                    str(paid_exc)[:500],
                )
                await _mark_source_health(conn, "degraded", str(paid_exc))
            return {"status": "zero_spend_rejected", "target": target, "observations": 0, "error": str(paid_exc), "dry_run": dry_run}
        if dry_run:
            return {"status": "dry_run", "target": target, "observations": 0, "error": error, "dry_run": True}
        await conn.execute(
            "UPDATE recon_targets SET status = 'failed', error = $2, updated_at = NOW() WHERE id = $1::uuid",
            target["id"],
            error,
        )
        await _mark_source_health(conn, "degraded", error)
        return {"status": "failed", "target": target, "observations": 0, "error": error, "dry_run": dry_run}
    if not modules:
        if dry_run:
            return {"status": "dry_run", "target": target, "modules": [], "observations": 0, "dry_run": True}
        await conn.execute(
            "UPDATE recon_targets SET status = 'blocked', error = $2, updated_at = NOW() WHERE id = $1::uuid",
            target["id"],
            "no SpiderFoot modules allowed by scope",
        )
        await _mark_source_health(conn, "running", None, success=True)
        return {"status": "blocked", "target": target, "observations": 0, "dry_run": dry_run}
    if dry_run:
        return {"status": "dry_run", "target": target, "modules": modules, "observations": 0, "dry_run": True}

    if _maigret_selected(target["target_type"], modules):
        try:
            fp_blocklist = await load_maigret_fp_blocklist(conn)
            observations, blocked_fp_sitenames = await _run_maigret_cli(
                target,
                _bounded_timeout_seconds(
                    "MAIGRET_TARGET_TIMEOUT_SECONDS",
                    int(DEFAULT_MAIGRET_TARGET_TIMEOUT_SECONDS),
                    600,
                ),
                worker_label=worker_label,
                fp_blocklist=fp_blocklist,
            )
            written = await _store_observations(conn, observations)
            if blocked_fp_sitenames:
                await bump_maigret_fp_hits(conn, blocked_fp_sitenames)
            if written == 0:
                return await _fail_empty_target(
                    conn, target, MAIGRET_MODULE, modules=modules,
                )
            await conn.execute(
                "UPDATE recon_targets SET status = 'completed', error = NULL, updated_at = NOW() WHERE id = $1::uuid",
                target["id"],
            )
            await _mark_source_health(conn, "running", None, success=True)
            return {
                "status": "completed",
                "target": target,
                "modules": modules,
                "engine": MAIGRET_MODULE,
                "observations": written,
                "fp_blocklist_size": len(fp_blocklist),
                "fp_filtered": len(blocked_fp_sitenames),
                "dry_run": False,
            }
        except Exception as exc:  # noqa: BLE001
            error = str(exc)[:500]
            await conn.execute(
                "UPDATE recon_targets SET status = 'failed', error = $2, updated_at = NOW() WHERE id = $1::uuid",
                target["id"],
                error,
            )
            # maigret timeouts are target-level; keep the recon service green.
            if "maigret timed out" in error or _is_target_level_failure(error):
                await _mark_source_health(conn, "running", None, success=True)
            else:
                await _mark_source_health(conn, "degraded", error)
            return {
                "status": "failed",
                "target": target,
                "modules": modules,
                "engine": MAIGRET_MODULE,
                "observations": 0,
                "error": error,
                "dry_run": False,
            }

    try:
        raw_rows = await _run_spiderfoot_cli(
            target,
            modules,
            _bounded_timeout_seconds("SPIDERFOOT_TARGET_TIMEOUT_SECONDS", 300, 600),
            worker_label=worker_label,
        )
        observations = [
            obs for raw in raw_rows
            if (obs := normalize_observation(target["id"], raw)) is not None
        ]
        written = await _store_observations(conn, observations)
        if written == 0:
            return await _fail_empty_target(
                conn, target, SOURCE_HEALTH_NAME, modules=modules,
            )
        await conn.execute(
            "UPDATE recon_targets SET status = 'completed', error = NULL, updated_at = NOW() WHERE id = $1::uuid",
            target["id"],
        )
        await _mark_source_health(conn, "running", None, success=True)
        return {"status": "completed", "target": target, "modules": modules, "observations": written, "dry_run": False}
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:500]
        await conn.execute(
            "UPDATE recon_targets SET status = 'failed', error = $2, updated_at = NOW() WHERE id = $1::uuid",
            target["id"],
            error,
        )
        if _is_target_level_failure(error):
            await _mark_source_health(conn, "running", None, success=True)
        else:
            await _mark_source_health(conn, "degraded", error)
        return {"status": "failed", "target": target, "modules": modules, "observations": 0, "error": error, "dry_run": False}
