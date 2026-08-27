"""Browser-platform activity rotator (plan task G3).

The shared CDP Chrome cannot scrape all browser platforms at once without tab
contention and detection walls (see plan v2 evidence). This rotator limits how
many browser-group collectors are actively scheduled at any moment by writing
the existing ``collection_schedules`` control plane (``enabled`` / ``next_run``)
that ``src/scheduler`` already consumes in ``_tick``:

    SELECT ... FROM collection_schedules WHERE enabled = true AND next_run <= now

Messaging + backend sources are PINNED and never touched, so WhatsApp / Telegram
/ Beeper stay realtime. Rotation is deterministic and stateless (time-slot
based), so multiple schedulers or restarts converge on the same active set.

Env:
  COLLECTOR_BROWSER_ROTATION_SOURCES   csv override of the browser group
  COLLECTOR_BROWSER_ROTATION_WIDTH     concurrent active platforms (default 2)
  COLLECTOR_BROWSER_ROTATION_INTERVAL_MINUTES  slot length (default 30)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any

# Browser-group collectors that share the one CDP Chrome. Order defines the
# round-robin sequence.
DEFAULT_BROWSER_SOURCES = [
    "x",
    "instagram",
    "facebook",
    "threads",
    "tiktok",
    "lemon8",
    "strava",
]


def _browser_sources() -> list[str]:
    env = os.getenv("COLLECTOR_BROWSER_ROTATION_SOURCES")
    if env:
        return [s.strip().lower() for s in env.split(",") if s.strip()]
    return list(DEFAULT_BROWSER_SOURCES)


def _rotation_width() -> int:
    try:
        return max(1, int(os.getenv("COLLECTOR_BROWSER_ROTATION_WIDTH", "2")))
    except (TypeError, ValueError):
        return 2


def _rotation_interval_seconds() -> int:
    try:
        return max(60, int(os.getenv("COLLECTOR_BROWSER_ROTATION_INTERVAL_MINUTES", "30")) * 60)
    except (TypeError, ValueError):
        return 1800


def select_active(sources: list[str], width: int, now: datetime, interval_seconds: int) -> list[str]:
    """Deterministic stateless round-robin: active sources for this time slot."""
    if not sources:
        return []
    width = max(1, min(width, len(sources)))
    slot = int(now.timestamp() // max(1, interval_seconds))
    start = (slot * width) % len(sources)
    return [sources[(start + i) % len(sources)] for i in range(width)]


async def rotate_browser_schedules(
    conn,
    *,
    sources: list[str] | None = None,
    width: int | None = None,
    now: datetime | None = None,
    interval_seconds: int | None = None,
) -> dict[str, Any]:
    """Activate the current slot's browser sources; pause the rest of the group.

    Only touches rows whose source is in the browser group — pinned messaging
    and backend schedules are never modified.
    """
    sources = sources if sources is not None else _browser_sources()
    width = width if width is not None else _rotation_width()
    interval_seconds = interval_seconds if interval_seconds is not None else _rotation_interval_seconds()
    now = now or datetime.now(timezone.utc)

    active = select_active(sources, width, now, interval_seconds)
    active_set = set(active)
    paused = [s for s in sources if s not in active_set]

    if active:
        await conn.execute(
            "UPDATE collection_schedules SET enabled = true, next_run = $2 "
            "WHERE source = ANY($1::text[])",
            list(active),
            now,
        )
    if paused:
        await conn.execute(
            "UPDATE collection_schedules SET enabled = false "
            "WHERE source = ANY($1::text[])",
            paused,
        )
    return {
        "active": sorted(active_set),
        "paused": sorted(paused),
        "width": width,
        "interval_seconds": interval_seconds,
        "slot_at": now.isoformat(),
    }


async def _run_once() -> None:
    from src.db.connection import get_pool  # local import: keep module import-light

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await rotate_browser_schedules(conn)
    print(json.dumps(result, default=str))


async def _run_loop() -> None:
    interval = _rotation_interval_seconds()
    while True:
        try:
            await _run_once()
        except Exception as exc:  # noqa: BLE001 - rotator must not crash the loop
            print(json.dumps({"status": "error", "error": str(exc)[:300]}))
        await asyncio.sleep(interval)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Browser-platform activity rotator")
    parser.add_argument("--loop", action="store_true", help="Run continuously every rotation interval")
    args = parser.parse_args(argv)
    asyncio.run(_run_loop() if args.loop else _run_once())


if __name__ == "__main__":
    main()
