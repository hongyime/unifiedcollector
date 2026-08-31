"""wa_device_sweep — READ-ONLY WhatsApp device-intelligence sweep.

Probes phone numbers we already hold (contacts-first via whatsapp_lid_map, then
wider) through the WhatsApp bridge /devices/<number> endpoint, which uses ONLY
onWhatsApp + getUSyncDevices (server-side USync queries). ZERO messages,
reactions, or calls are ever sent to any contact — probing is invisible to them.

Results persist to:
  - wa_device_observations  (append: one row per probe)
  - wa_devices              (upsert: current per-device state)

Run (inside a collector container that can reach host.docker.internal):
  python -m src.core.wa_device_sweep [--limit N] [--dry-run]

Scheduled ~every 4h. Idempotent + gentle: least-recently-probed numbers first,
bounded batch, jittered spacing, round-robin across the ready bridges.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import re

import asyncpg
import httpx

BRIDGES: list[tuple[str, str]] = [
    ("account1", os.getenv("WA_BRIDGE_1_URL", "http://host.docker.internal:3011")),
    ("account2", os.getenv("WA_BRIDGE_2_URL", "http://host.docker.internal:3012")),
]
BATCH = int(os.getenv("WA_SWEEP_BATCH", "40"))
JITTER_MIN = float(os.getenv("WA_SWEEP_JITTER_MIN", "8"))
JITTER_MAX = float(os.getenv("WA_SWEEP_JITTER_MAX", "18"))
PROBE_TIMEOUT = float(os.getenv("WA_SWEEP_PROBE_TIMEOUT", "45"))
# A known on-WhatsApp number used ONLY to verify a bridge actually has the
# /devices endpoint. Older bridge builds (not restarted after the device_intel
# deploy) 404 it; probing through them would waste half the batch. (Our own
# account2 number — not an external contact.)
HEALTHCHECK_NUMBER = os.getenv("WA_SWEEP_HEALTHCHECK_NUMBER", "6584731565")


def _dsn() -> str:
    return os.environ["DATABASE_URL"]


async def _ready_bridges(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    ready: list[tuple[str, str]] = []
    for name, base in BRIDGES:
        try:
            r = await client.get(f"{base}/health", timeout=8)
            if not (r.status_code == 200 and r.json().get("whatsapp_ready")):
                continue
            # Verify /devices actually exists on this bridge build (old builds 404).
            probe = await client.get(f"{base}/devices/{HEALTHCHECK_NUMBER}", timeout=20)
            if probe.status_code == 200:
                ready.append((name, base))
            else:
                print(f"[wa_sweep] skip {name}: /devices -> {probe.status_code} (endpoint missing / not restarted)")
        except Exception as e:
            print(f"[wa_sweep] skip {name}: {e}")
    return ready


async def _select_batch(conn: asyncpg.Connection, limit: int) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT m.phone_jid
        FROM whatsapp_lid_map m
        LEFT JOIN LATERAL (
            SELECT max(observed_at) AS last_obs
            FROM wa_device_observations o
            WHERE o.phone_jid = m.phone_jid
        ) o ON true
        WHERE m.phone_jid LIKE '%@s.whatsapp.net'
        ORDER BY o.last_obs ASC NULLS FIRST
        LIMIT $1
        """,
        limit,
    )
    return [r["phone_jid"] for r in rows]


async def _probe(client: httpx.AsyncClient, base: str, number: str) -> dict | None:
    try:
        r = await client.get(f"{base}/devices/{number}", timeout=PROBE_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


async def _persist(conn: asyncpg.Connection, phone_jid: str, probed_by: str, res: dict) -> None:
    exists = bool(res.get("exists"))
    devices = res.get("devices") or []
    device_ids = sorted({int(d.get("device", 0)) for d in devices if isinstance(d, dict)})
    await conn.execute(
        "INSERT INTO wa_device_observations (phone_jid, probed_by, exists_on_wa, device_count, device_ids) "
        "VALUES ($1,$2,$3,$4,$5)",
        phone_jid, probed_by, exists, len(device_ids), device_ids,
    )
    for did in device_ids:
        await conn.execute(
            "INSERT INTO wa_devices (phone_jid, device_id, exists_on_wa, first_seen, last_seen) "
            "VALUES ($1,$2,$3,now(),now()) "
            "ON CONFLICT (phone_jid, device_id) DO UPDATE SET last_seen=now(), exists_on_wa=EXCLUDED.exists_on_wa",
            phone_jid, did, exists,
        )


async def run(limit: int, dry_run: bool) -> None:
    conn = await asyncpg.connect(_dsn(), command_timeout=120)
    try:
        async with httpx.AsyncClient() as client:
            ready = await _ready_bridges(client)
            if not ready:
                print("[wa_sweep] no ready bridges; abort")
                return
            print(f"[wa_sweep] ready bridges: {[n for n, _ in ready]}")
            batch = await _select_batch(conn, limit)
            print(f"[wa_sweep] batch={len(batch)} (contacts-first, least-recently-probed)")
            ok = miss = 0
            for i, phone_jid in enumerate(batch):
                number = re.sub(r"[^0-9]", "", phone_jid.split("@")[0])
                if not number:
                    continue
                name, base = ready[i % len(ready)]
                res = await _probe(client, base, number)
                if not res:
                    miss += 1
                    continue
                if dry_run:
                    print(f"  [dry] {number} via {name}: exists={res.get('exists')} devices={res.get('device_count')}")
                else:
                    await _persist(conn, phone_jid, res.get("session_name") or name, res)
                ok += 1
                await asyncio.sleep(random.uniform(JITTER_MIN, JITTER_MAX))
            print(f"[wa_sweep] done probed_ok={ok} misses={miss} dry_run={dry_run}")
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only WhatsApp device-intel sweep")
    ap.add_argument("--limit", type=int, default=BATCH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args.limit, args.dry_run))


if __name__ == "__main__":
    main()
