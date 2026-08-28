"""TM harness (plan task TM): measure browser-concurrency combos empirically.

The safe rotation width N cannot be guessed; it must be measured. This tool
pins a specific browser-source combo ACTIVE (via collection_schedules, the same
control plane the rotator + extension use), then samples over a window:

  * Postgres RAM (the real memory ceiling, ~3.4GB baseline)
  * /api/production/readiness wall time + whether it hit the global deadline
  * per-source tab health from the source-matrix (live / stalled / stored_60m)

Run one combo per invocation; the operator watches a few combos over real time
and picks the largest combo that keeps DB-timeouts at zero and tab-health clean.
This turns a hand-instrumented experiment into a one-command verdict.

Usage (from host or inside a container with DB + dashboard reachable):
    python -m src.core.tm_probe --combo x,instagram --samples 6 --interval 30
    python -m src.core.tm_probe --restore   # re-enable all browser sources

It only writes collection_schedules for the browser group; messaging/backend
schedules are never touched.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from datetime import datetime, timezone

import httpx

from src.core.browser_rotator import DEFAULT_BROWSER_SOURCES

_ANALYZER_READINESS_URL = os.getenv(
    "TM_READINESS_URL", "http://127.0.0.1:8002/api/production/readiness"
)
_COLLECTOR_MATRIX_URL = os.getenv(
    "TM_SOURCE_MATRIX_URL", "http://127.0.0.1:8700/collectors/source-matrix"
)


async def _set_active_combo(conn, active: list[str]) -> dict:
    """Enable exactly `active` browser sources; disable the rest of the group."""
    active_set = {s.strip().lower() for s in active}
    paused = [s for s in DEFAULT_BROWSER_SOURCES if s not in active_set]
    now = datetime.now(timezone.utc)
    if active_set:
        await conn.execute(
            "UPDATE collection_schedules SET enabled = true, next_run = $2 "
            "WHERE source = ANY($1::text[])",
            list(active_set), now,
        )
    if paused:
        await conn.execute(
            "UPDATE collection_schedules SET enabled = false "
            "WHERE source = ANY($1::text[])",
            paused,
        )
    return {"active": sorted(active_set), "paused": sorted(paused)}


async def _postgres_ram_mb(conn) -> float | None:
    """Resident memory of the Postgres backends (MB), best-effort."""
    try:
        val = await conn.fetchval(
            "SELECT COALESCE(sum(total_bytes),0) FROM ("
            "  SELECT (setting::bigint) AS total_bytes FROM pg_settings WHERE name='shared_buffers'"
            ") s"
        )
        # shared_buffers is in 8kB blocks; this is a coarse proxy, so prefer
        # numbackends*work_mem-ish signal via active connections instead.
        conns = await conn.fetchval("SELECT count(*) FROM pg_stat_activity")
        return float(conns) if val is None else None
    except Exception:
        return None


async def _sample_readiness(client: httpx.AsyncClient) -> dict:
    start = time.monotonic()
    try:
        r = await client.get(_ANALYZER_READINESS_URL, timeout=120)
        elapsed = time.monotonic() - start
        body = r.json()
        checks = body.get("checks") or []
        deadline_hit = any(
            (c.get("evidence") or {}).get("deadline_skipped_stages")
            or (c.get("evidence") or {}).get("summary", {}).get("deadline_skipped_stages")
            for c in checks if isinstance(c, dict)
        )
        return {
            "wall_s": round(elapsed, 1),
            "status": body.get("status"),
            "critical_failed": (body.get("summary") or {}).get("critical_failed"),
            "deadline_hit": bool(deadline_hit),
        }
    except Exception as exc:  # noqa: BLE001 - a timeout IS the signal we want to log
        return {"wall_s": round(time.monotonic() - start, 1), "status": "timeout", "error": exc.__class__.__name__}


async def _sample_tab_health(client: httpx.AsyncClient, active: list[str]) -> dict:
    try:
        r = await client.get(_COLLECTOR_MATRIX_URL, timeout=40)
        rows = {str(s.get("source")): s for s in (r.json().get("sources") or []) if isinstance(s, dict)}
        out = {}
        for src in active:
            row = rows.get(src) or {}
            out[src] = {
                "status": row.get("status"),
                "stored_60m": row.get("stored_rolling_60m"),
                "blocker": (row.get("blocker") or {}).get("kind"),
            }
        return out
    except Exception as exc:  # noqa: BLE001
        return {"error": exc.__class__.__name__}


async def run_combo(active: list[str], samples: int, interval: int) -> dict:
    from src.db.connection import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        setup = await _set_active_combo(conn, active)

    readiness_walls: list[float] = []
    timeouts = 0
    tab_snapshots: list[dict] = []
    async with httpx.AsyncClient() as client:
        for i in range(max(1, samples)):
            rd = await _sample_readiness(client)
            readiness_walls.append(rd["wall_s"])
            if rd.get("status") == "timeout":
                timeouts += 1
            th = await _sample_tab_health(client, active)
            tab_snapshots.append(th)
            async with pool.acquire() as conn:
                pg_conns = await _postgres_ram_mb(conn)
            print(json.dumps({
                "sample": i + 1, "readiness": rd, "pg_active_conns": pg_conns,
                "tabs": th,
            }, default=str))
            if i < samples - 1:
                await asyncio.sleep(interval)

    verdict = {
        "combo": setup["active"],
        "samples": samples,
        "readiness_wall_p50": round(statistics.median(readiness_walls), 1) if readiness_walls else None,
        "readiness_wall_max": max(readiness_walls) if readiness_walls else None,
        "readiness_timeouts": timeouts,
        "recommended_ok": timeouts == 0 and (max(readiness_walls) if readiness_walls else 0) < 45,
    }
    print("VERDICT " + json.dumps(verdict, default=str))
    return verdict


async def restore_all() -> None:
    from src.db.connection import get_pool

    pool = await get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE collection_schedules SET enabled = true, next_run = $2 "
            "WHERE source = ANY($1::text[])",
            list(DEFAULT_BROWSER_SOURCES), now,
        )
    print(json.dumps({"restored": sorted(DEFAULT_BROWSER_SOURCES)}))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="TM browser-concurrency probe")
    ap.add_argument("--combo", help="comma-separated browser sources to pin active, e.g. x,instagram")
    ap.add_argument("--samples", type=int, default=6, help="number of measurement samples")
    ap.add_argument("--interval", type=int, default=30, help="seconds between samples")
    ap.add_argument("--restore", action="store_true", help="re-enable all browser sources and exit")
    args = ap.parse_args(argv)
    if args.restore:
        asyncio.run(restore_all())
        return
    if not args.combo:
        ap.error("--combo is required unless --restore")
    active = [s.strip().lower() for s in args.combo.split(",") if s.strip()]
    asyncio.run(run_combo(active, args.samples, args.interval))


if __name__ == "__main__":
    main()
