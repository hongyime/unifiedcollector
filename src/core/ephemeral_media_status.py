"""Aggregate story/highlight status shared by dashboard and control room."""
from __future__ import annotations


async def get_ephemeral_media_status(pool, *, limit: int = 300) -> dict:
    limit = max(1, min(limit, 2000))
    async with pool.acquire() as conn:
        stats = await conn.fetchrow(
            """
            SELECT count(*) FILTER (WHERE kind='story') AS stories,
                   count(*) FILTER (WHERE kind='highlight') AS highlights,
                   count(DISTINCT entity_name) AS accounts,
                   count(DISTINCT source) AS sources,
                   max(collected_at) AS newest
            FROM media_items WHERE kind IN ('story', 'highlight')
            """,
        )
        rows = await conn.fetch(
            """
            SELECT source, entity_name,
                   count(*) FILTER (WHERE kind='story') AS story_count,
                   count(*) FILTER (WHERE kind='highlight') AS highlight_count,
                   count(*) AS total, max(collected_at) AS newest
            FROM media_items WHERE kind IN ('story', 'highlight')
            GROUP BY source, entity_name
            ORDER BY max(collected_at) DESC NULLS LAST LIMIT $1
            """, limit,
        )
    return {"stats": dict(stats) if stats else {}, "accounts": [dict(row) for row in rows]}
