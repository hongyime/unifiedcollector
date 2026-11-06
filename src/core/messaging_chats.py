"""Bounded native messaging-chat listings for dashboard and control room."""
from __future__ import annotations


async def list_messaging_chats(
    pool, *, source: str = "telegram", limit: int = 100, network: str | None = None,
) -> list[dict]:
    source = (source or "telegram").strip().lower()
    limit = max(1, min(limit, 500))
    async with pool.acquire() as conn:
        if source == "telegram":
            if await conn.fetchval("SELECT to_regclass('telegram_chats')") is None:
                return []
            rows = await conn.fetch(
                """
                SELECT platform_chat_id, title, username, type, description, members_count, updated_at, collected_at
                FROM telegram_chats ORDER BY updated_at DESC NULLS LAST LIMIT $1
                """, limit,
            )
        elif source == "whatsapp":
            if await conn.fetchval("SELECT to_regclass('whatsapp_chats')") is None:
                return []
            rows = await conn.fetch(
                """
                SELECT platform_chat_id, name AS title, NULL::text AS username, chat_type AS type,
                       description, participant_count AS members_count, updated_at, collected_at
                FROM whatsapp_chats ORDER BY updated_at DESC NULLS LAST LIMIT $1
                """, limit,
            )
        elif source == "beeper":
            if await conn.fetchval("SELECT to_regclass('beeper_shadow_chats')") is None:
                return []
            where = ""
            args: list = [limit]
            if network:
                args.append(network)
                where = "WHERE c.network = $2"
            rows = await conn.fetch(
                f"""
                SELECT c.chat_id,
                       c.local_chat_id,
                       c.network,
                       c.title,
                       c.img_url,
                       c.chat_type,
                       (c.chat_type = 'dm') AS is_direct,
                       c.account_id,
                       c.last_seen_at,
                       c.chat_id AS platform_chat_id,
                       c.chat_type AS type,
                       (SELECT COUNT(*) FROM beeper_shadow_messages WHERE chat_id = c.chat_id) AS messages_collected,
                       (SELECT MAX(timestamp) FROM beeper_shadow_messages WHERE chat_id = c.chat_id) AS last_message_at
                FROM beeper_shadow_chats c
                {where}
                ORDER BY c.last_seen_at DESC NULLS LAST
                LIMIT $1
                """,
                *args,
            )
        else:
            raise ValueError("unsupported messaging source")
    return [dict(row) for row in rows]
