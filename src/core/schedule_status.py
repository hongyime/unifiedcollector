"""Shared read-only collection schedule status for dashboard and control room."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


async def list_collection_schedules(pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM collection_schedules ORDER BY source")
    return [dict(row) for row in rows]


async def set_collection_schedule(pool, source: str, *, interval_hours: int, enabled: bool) -> dict:
    """Upsert a bounded schedule without exposing schedule deletion."""
    interval_hours = max(1, min(int(interval_hours), 720))
    next_run = datetime.now(timezone.utc) + timedelta(hours=interval_hours)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO collection_schedules (source, interval_hours, enabled, next_run) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (source) DO UPDATE "
            "SET interval_hours = $2, enabled = $3, next_run = $4",
            source,
            interval_hours,
            enabled,
            next_run,
        )
    return {
        "source": source,
        "interval_hours": interval_hours,
        "enabled": enabled,
        "next_run": next_run.isoformat(),
    }
