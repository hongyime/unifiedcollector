"""Bounded native message history shared by dashboard and control room."""
from __future__ import annotations


async def get_messaging_messages(pool, *, source: str, chat_id: str, limit: int = 200) -> dict:
    source = (source or "").strip().lower()
    chat_id = (chat_id or "").strip()
    limit = max(1, min(limit, 2000))
    if source not in {"telegram", "whatsapp", "beeper"}:
        raise ValueError("unsupported messaging source")
    async with pool.acquire() as conn:
        if source == "telegram":
            if await conn.fetchval("SELECT to_regclass('telegram_chats')") is None:
                return {"chat": None, "messages": []}
            chat_row = await conn.fetchrow(
                """
                SELECT id, platform_chat_id, title, username, type, description,
                       members_count, updated_at, collected_at
                FROM telegram_chats WHERE platform_chat_id=$1
                """, chat_id,
            )
            if not chat_row:
                return {"chat": None, "messages": []}
            chat = dict(chat_row)
            chat_uuid = chat.pop("id")
            chat["message_count"] = int(await conn.fetchval(
                "SELECT COUNT(*) FROM telegram_messages WHERE chat_id=$1", chat_uuid,
            ) or 0)
            rows = await conn.fetch(
                """
                WITH picked AS (
                    SELECT id FROM telegram_messages WHERE chat_id=$1
                    ORDER BY platform_created_at DESC NULLS LAST, collected_at DESC LIMIT $3
                )
                SELECT m.platform_message_id, m.text, m.caption, m.media_type, m.media_file_id,
                       m.is_edited, m.edit_date, m.reply_to_message_id, m.platform_created_at,
                       m.collected_at, (m.metadata->>'deleted' = 'true' IS TRUE) AS is_deleted,
                       m.metadata->>'deleted_at' AS deleted_at, u.platform_user_id AS sender_platform_id,
                       u.username AS sender_username, u.first_name AS sender_first_name,
                       u.last_name AS sender_last_name, mi.id AS media_item_id
                FROM picked p JOIN telegram_messages m ON m.id=p.id
                LEFT JOIN telegram_users u ON u.id=m.sender_id
                LEFT JOIN media_items mi ON mi.source='telegram' AND mi.entity_id=$2
                    AND mi.content_id=split_part(m.platform_message_id, ':', 2)
                ORDER BY m.platform_created_at DESC NULLS LAST, m.collected_at DESC
                """, chat_uuid, chat_id, limit,
            )
            return {"chat": chat, "messages": [dict(row) for row in rows]}
        if source == "beeper":
            if await conn.fetchval("SELECT to_regclass('beeper_shadow_chats')") is None:
                return {"chat": None, "messages": []}
            chat_row = await conn.fetchrow(
                """
                SELECT c.chat_id, c.local_chat_id, c.network, c.title, c.img_url, c.chat_type,
                       (c.chat_type = 'dm') AS is_direct, c.account_id, c.last_seen_at
                FROM beeper_shadow_chats c WHERE c.chat_id=$1
                """,
                chat_id,
            )
            if not chat_row:
                return {"chat": None, "messages": []}
            messages = await conn.fetch(
                """
                SELECT m.message_id, m.network, m.sender_id, m.sender_name, m.text, m.timestamp,
                       m.sort_key,
                       (m.msg_type IN ('IMAGE','VIDEO','STICKER','FILE','VOICE','AUDIO')
                        OR (m.attachments IS NOT NULL AND m.attachments::text NOT IN ('null','[]','{}'))) AS is_media,
                       NULL::text AS media_url, m.msg_type AS media_type, m.is_deleted, m.deleted_at, m.ingested_at
                FROM beeper_shadow_messages m WHERE m.chat_id=$1
                ORDER BY m.timestamp DESC NULLS LAST LIMIT $2
                """,
                chat_id, limit,
            )
            mids = [row["message_id"] for row in messages]
            media_map: dict[str, str] = {}
            if mids:
                media_rows = await conn.fetch(
                    """
                    SELECT DISTINCT ON (split_part(content_id, '_', 1))
                           split_part(content_id, '_', 1) AS mid, id AS media_item_id
                    FROM media_items WHERE source='beeper'
                      AND split_part(content_id, '_', 1) = ANY($1::text[])
                    ORDER BY split_part(content_id, '_', 1), collected_at
                    """,
                    mids,
                )
                media_map = {row["mid"]: row["media_item_id"] for row in media_rows}
            return {
                "chat": dict(chat_row),
                "messages": [dict(row, media_item_id=media_map.get(row["message_id"])) for row in reversed(messages)],
            }
        if await conn.fetchval("SELECT to_regclass('whatsapp_chats')") is None:
            return {"chat": None, "messages": []}
        chat_row = await conn.fetchrow(
            """
            SELECT platform_chat_id, name, is_group, chat_type, participant_count,
                   description, updated_at FROM whatsapp_chats WHERE platform_chat_id=$1
            """, chat_id,
        )
        if not chat_row:
            return {"chat": None, "messages": []}
        rows = await conn.fetch(
            """
            WITH msgs AS (
                SELECT m.* FROM whatsapp_messages m
                WHERE m.chat_id=(SELECT id FROM whatsapp_chats WHERE platform_chat_id=$1)
                ORDER BY m."timestamp" DESC NULLS LAST LIMIT $2
            )
            SELECT m.platform_message_id, m.from_me, m.text, m.media_url, m.media_mime_type,
                   m.media_size, m.thumbnail_url, m."timestamp", m.is_deleted, m.deleted_at,
                   m.quoted_text, m.forward_from_name, u.platform_user_id AS sender_jid,
                   u.pushname AS sender_pushname, u.name AS sender_name, u.phone_number AS sender_phone,
                   mi.id::text AS media_id
            FROM msgs m LEFT JOIN whatsapp_users u ON u.id=m.sender_id
            LEFT JOIN LATERAL (
                SELECT mi.id FROM media_items mi
                WHERE mi.source='whatsapp' AND (mi.content_id='wa_' || m.platform_message_id
                    OR (m.media_url IS NOT NULL AND mi.file_path=m.media_url))
                ORDER BY CASE WHEN mi.content_id='wa_' || m.platform_message_id THEN 0 ELSE 1 END,
                         mi.collected_at DESC LIMIT 1
            ) mi ON TRUE
            ORDER BY m."timestamp" DESC NULLS LAST
            """, chat_id, limit,
        )
    messages = []
    for row in reversed(rows):
        message = dict(row)
        text = message.get("text")
        if text is not None and len(text) > 1500:
            message["text"] = text[:1500]
            message["text_truncated"] = True
            message["text_full_length"] = len(text)
        messages.append(message)
    return {"chat": dict(chat_row), "messages": messages}
