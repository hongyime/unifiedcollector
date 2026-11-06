"""Bounded native chat-member rosters for dashboard and control room."""
from __future__ import annotations


async def list_messaging_members(pool, *, source: str, chat_id: str, limit: int = 100) -> list[dict]:
    source = (source or "").strip().lower()
    chat_id = (chat_id or "").strip()
    limit = max(1, min(limit, 500))
    if source not in {"telegram", "whatsapp", "beeper"}:
        raise ValueError("unsupported messaging source")
    async with pool.acquire() as conn:
        if source == "telegram":
            rows = await conn.fetch(
                """
                SELECT u.platform_user_id AS member_id, u.username, u.first_name, u.last_name,
                       u.is_bot, m.role, m.joined_at, m.last_seen_at, m.refreshed_at
                FROM telegram_chat_members m
                JOIN telegram_chats c ON c.id=m.chat_id
                JOIN telegram_users u ON u.id=m.user_id
                WHERE c.platform_chat_id=$1
                ORDER BY m.last_seen_at DESC NULLS LAST, u.username NULLS LAST, u.platform_user_id
                LIMIT $2
                """, chat_id, limit,
            )
        elif source == "whatsapp":
            rows = await conn.fetch(
                """
                SELECT m.participant_jid AS member_id, u.name, u.pushname, u.phone_number,
                       u.is_business, m.role, m.first_seen_at AS joined_at, m.last_seen_at
                FROM whatsapp_group_members m
                LEFT JOIN whatsapp_users u ON u.platform_user_id=m.participant_jid
                WHERE m.platform_chat_id=$1 AND m.is_active=true
                ORDER BY m.last_seen_at DESC NULLS LAST, m.participant_jid
                LIMIT $2
                """, chat_id, limit,
            )
        else:
            if await conn.fetchval("SELECT to_regclass('beeper_shadow_participants')") is None:
                return []
            rows = await conn.fetch(
                """
                SELECT participant_id AS member_id, username, full_name AS name,
                       CASE WHEN is_admin THEN 'admin' WHEN is_self THEN 'self' ELSE 'member' END AS role,
                       is_network_bot, is_pending, cannot_message,
                       first_seen_at AS joined_at, last_seen_at
                FROM beeper_shadow_participants
                WHERE chat_id=$1
                ORDER BY last_seen_at DESC NULLS LAST, participant_id
                LIMIT $2
                """, chat_id, limit,
            )
    return [dict(row) for row in rows]
