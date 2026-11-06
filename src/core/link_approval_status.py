"""Read-only reviewer-gated link approval status shared by dashboard and bot."""
from __future__ import annotations


async def list_link_approvals(pool, *, source: str | None = None, limit: int = 100) -> list[dict]:
    source = (source or "").strip().lower() or None
    limit = max(1, min(limit, 500))
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('link_approvals')") is None:
            return []
        rows = await conn.fetch(
            """
            SELECT source, target_url, account_name, state, requested_at, approved_at,
                   started_at, completed_at, expires_at, last_error, updated_at
            FROM link_approvals
            WHERE ($1::text IS NULL OR source=$1)
            ORDER BY updated_at DESC
            LIMIT $2
            """,
            source, limit,
        )
    return [dict(row) for row in rows]
