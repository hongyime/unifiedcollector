"""Bounded source-control state shared by dashboard and control room."""
from __future__ import annotations

from datetime import datetime, timezone


async def is_manually_paused(pool, source: str) -> str | None:
    """Return an operator pause reason; provider cooldowns remain automatic."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, enabled, stop_reason FROM source_controls WHERE source_name=$1", source,
            )
    except Exception:
        return None
    if row and (row["state"] == "PAUSED" or not row["enabled"]):
        return str(row["stop_reason"] or "operator pause")
    return None


async def automatic_cooldown_seconds(pool, source: str) -> float:
    """Return remaining provider cooldown; operator pauses use ``is_manually_paused``."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, blocked_until FROM source_controls WHERE source_name=$1", source,
            )
    except Exception:
        return 0.0
    blocked_until = row and row["blocked_until"]
    if row and row["state"] in {"RATE_LIMITED", "AUTH_FAILED"} and blocked_until:
        return max(0.0, (blocked_until - datetime.now(timezone.utc)).total_seconds())
    return 0.0


async def set_manual_pause(pool, source: str, *, paused: bool, reason: str | None = None) -> None:
    """Pause or explicitly resume a source without treating it as provider failure."""
    async with pool.acquire() as conn:
        if paused:
            await conn.execute(
                """INSERT INTO source_controls(source_name,state,enabled,stop_reason,updated_at)
                   VALUES($1,'PAUSED',false,$2,now())
                   ON CONFLICT(source_name) DO UPDATE SET state='PAUSED',enabled=false,
                       stop_reason=EXCLUDED.stop_reason,blocked_until=NULL,updated_at=now()""",
                source, reason or "operator pause",
            )
        else:
            await conn.execute(
                """UPDATE source_controls SET state='HEALTHY',enabled=true,stop_reason=NULL,
                       blocked_until=NULL,consecutive_failures=0,updated_at=now()
                   WHERE source_name=$1""",
                source,
            )


async def list_source_controls(pool, *, limit: int = 100) -> list[dict]:
    limit = max(1, min(limit, 500))
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('source_controls')") is None:
            return []
        rows = await conn.fetch(
            """
            SELECT source_name, state, enabled, last_success, last_attempt, blocked_until,
                   stop_reason, consecutive_failures, updated_at
            FROM source_controls ORDER BY source_name LIMIT $1
            """,
            limit,
        )
    return [
        {
            **dict(row),
            **{
                key: row[key].isoformat() if row.get(key) else None
                for key in ("last_success", "last_attempt", "blocked_until", "updated_at")
            },
        }
        for row in rows
    ]
