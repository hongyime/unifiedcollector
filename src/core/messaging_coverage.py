"""Canonical native-versus-Beeper messaging coverage read model."""
from __future__ import annotations


def _network(value: object) -> str:
    text = str(value or "").strip()
    return text if text and text.lower() != "unknown" else "Unmapped Beeper"


async def get_messaging_coverage(pool) -> list[dict]:
    async with pool.acquire() as conn:
        telegram_people = await conn.fetchrow(
            "SELECT count(*) FILTER (WHERE COALESCE(is_bot,false)=false) AS people, count(*) FILTER (WHERE is_bot=true) AS bots FROM telegram_users",
        )
        native = {
            "Telegram": {
                "messages": int(await conn.fetchval("SELECT COUNT(*) FROM telegram_messages") or 0),
                "chats": int(await conn.fetchval("SELECT COUNT(*) FROM telegram_chats") or 0),
                "people": int((telegram_people or {}).get("people") or 0),
                "bots": int((telegram_people or {}).get("bots") or 0),
                "last_message": await conn.fetchval("SELECT max(platform_created_at) FROM telegram_messages"),
            },
            "WhatsApp": {
                "messages": int(await conn.fetchval("SELECT COUNT(*) FROM whatsapp_messages") or 0),
                "chats": int(await conn.fetchval("SELECT COUNT(*) FROM whatsapp_chats") or 0),
                "people": int(await conn.fetchval("SELECT COUNT(*) FROM whatsapp_users") or 0),
                "bots": 0,
                "last_message": await conn.fetchval('SELECT max("timestamp") FROM whatsapp_messages'),
            },
        }
        message_rows = await conn.fetch(
            """
            SELECT COALESCE(NULLIF(trim(network), ''), 'Unmapped Beeper') AS network,
                   count(*)::bigint AS messages, count(DISTINCT chat_id)::int AS chats,
                   max(timestamp) AS last_message
            FROM beeper_shadow_messages GROUP BY 1
            """,
        )
        chat_rows = await conn.fetch(
            """
            SELECT COALESCE(NULLIF(trim(network), ''), 'Unmapped Beeper') AS network,
                   count(*)::int AS chats, max(last_seen_at) AS last_seen
            FROM beeper_shadow_chats GROUP BY 1
            """,
        )
        people_rows = await conn.fetch(
            """
            SELECT COALESCE(NULLIF(trim(network), ''), 'Unmapped Beeper') AS network,
                   count(DISTINCT participant_id)::int AS people
            FROM beeper_shadow_participants GROUP BY 1
            """,
        )
    beeper_messages = {_network(row["network"]): dict(row) for row in message_rows}
    beeper_chats = {_network(row["network"]): dict(row) for row in chat_rows}
    beeper_people = {_network(row["network"]): dict(row) for row in people_rows}
    networks = ["Telegram", "WhatsApp"] + sorted(
        (set(beeper_messages) | set(beeper_chats) | set(beeper_people)) - {"Telegram", "WhatsApp"},
        key=str.lower,
    )
    out = []
    for network in networks:
        native_stats = native.get(network, {})
        mirror = beeper_messages.get(network, {})
        mirror_chats = beeper_chats.get(network, {})
        mirror_people = beeper_people.get(network, {})
        source = {"Telegram": "telegram", "WhatsApp": "whatsapp"}.get(network)
        native_last = native_stats.get("last_message")
        beeper_last = mirror.get("last_message") or mirror_chats.get("last_seen")
        out.append({
            "network": network, "beeper_network": network, "native_source": source,
            "canonical_source": "native" if source else "beeper",
            "policy": f"{source} native is canonical; Beeper is a mirror/backstop" if source else "Beeper is canonical until a native collector exists",
            "native_messages": native_stats.get("messages", 0), "native_chats": native_stats.get("chats", 0),
            "native_people": native_stats.get("people", 0), "native_people_basis": "native users" if source else None,
            "native_bots": native_stats.get("bots", 0),
            "native_last_message": native_last.isoformat() if native_last else None,
            "beeper_messages": int(mirror.get("messages") or 0),
            "beeper_chats": int(mirror_chats.get("chats") or mirror.get("chats") or 0),
            "beeper_people": int(mirror_people.get("people") or 0),
            "beeper_people_basis": "beeper_shadow_participants" if mirror_people else None,
            "beeper_message_senders": None,
            "beeper_last_message": beeper_last.isoformat() if beeper_last else None,
            "coverage_note": "Native rows are canonical; Beeper rows are a mirror/backstop and are not additive." if source else None,
        })
    return out
