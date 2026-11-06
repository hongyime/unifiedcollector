"""Bounded dead-letter queue read model shared by dashboard and control room."""
from __future__ import annotations


async def list_dead_letter_queue(pool, *, source: str | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(limit, 500))
    source = (source or "").strip().lower() or None
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('public.dead_letter_queue')") is None:
            return []
        if source:
            rows = await conn.fetch(
                """
                SELECT id, source, entity_id, content_id, error_message, retry_count,
                       status, next_retry_at, last_attempt_at, created_at
                FROM dead_letter_queue WHERE source=$1
                ORDER BY created_at DESC LIMIT $2
                """, source, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, source, entity_id, content_id, error_message, retry_count,
                       status, next_retry_at, last_attempt_at, created_at
                FROM dead_letter_queue ORDER BY created_at DESC LIMIT $1
                """, limit,
            )
    return [dict(row) for row in rows]
