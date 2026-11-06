"""Bounded canonical reaction and poll read model."""
from __future__ import annotations


async def get_messaging_interactions(pool, *, source: str, limit: int = 30) -> dict:
    source = (source or "").strip().lower()
    limit = max(1, min(limit, 100))
    if source not in {"telegram", "whatsapp"}:
        raise ValueError("unsupported messaging source")
    async with pool.acquire() as conn:
        if source == "telegram":
            reactions = [dict(row) for row in await conn.fetch(
                """
                SELECT m.platform_message_id AS message_id, COALESCE(u.username, u.platform_user_id) AS actor,
                       r.emoji, r.added_at AS occurred_at
                FROM telegram_reactions r JOIN telegram_messages m ON m.id=r.message_id
                LEFT JOIN telegram_users u ON u.id=r.user_id
                ORDER BY r.refreshed_at DESC LIMIT $1
                """, limit,
            )]
            polls = [dict(row) for row in await conn.fetch(
                """
                SELECT m.platform_message_id AS message_id, p.question, p.total_voters,
                       p.is_closed, p.refreshed_at AS occurred_at
                FROM telegram_polls p JOIN telegram_messages m ON m.id=p.message_id
                ORDER BY p.refreshed_at DESC LIMIT $1
                """, limit,
            )]
        else:
            reactions = [dict(row) for row in await conn.fetch(
                """
                SELECT target_message_id AS message_id, actor_jid AS actor, emoji,
                       COALESCE(removed_at, reacted_at) AS occurred_at, removed_at IS NOT NULL AS removed
                FROM whatsapp_reactions ORDER BY updated_at DESC LIMIT $1
                """, limit,
            )]
            polls = [dict(row) for row in await conn.fetch(
                """
                SELECT platform_message_id AS message_id, question, NULL::bigint AS total_voters,
                       FALSE AS is_closed, created_at AS occurred_at
                FROM whatsapp_polls ORDER BY updated_at DESC LIMIT $1
                """, limit,
            )]
    return {"reactions": reactions, "polls": polls}
