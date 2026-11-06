"""Bounded WhatsApp-user reads shared by the dashboard and control room."""
from __future__ import annotations


async def list_whatsapp_users(pool, *, search: str | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(limit, 500))
    search = (search or "").strip()
    async with pool.acquire() as conn:
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = await conn.fetch(
                "SELECT * FROM whatsapp_users "
                "WHERE platform_user_id ILIKE $1 ESCAPE '\\' OR name ILIKE $1 ESCAPE '\\' "
                "OR pushname ILIKE $1 ESCAPE '\\' "
                "ORDER BY updated_at DESC NULLS LAST LIMIT $2",
                f"%{escaped}%", limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM whatsapp_users ORDER BY updated_at DESC NULLS LAST LIMIT $1",
                limit,
            )
    return [dict(row) for row in rows]


async def get_whatsapp_user_history(pool, *, jid: str, limit: int = 100) -> list[dict]:
    limit = max(1, min(limit, 1000))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT m.* FROM whatsapp_messages m "
            "JOIN whatsapp_users u ON u.id = m.sender_id "
            "WHERE u.platform_user_id = $1 "
            "ORDER BY m.collected_at DESC LIMIT $2",
            jid.strip(), limit,
        )
    return [dict(row) for row in rows]
