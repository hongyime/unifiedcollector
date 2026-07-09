"""One-off backfill: relink orphaned telegram_messages to their chat.

telegram_messages.chat_id is a UUID FK -> telegram_chats.id. _upsert_message
set it from a lookup on telegram_chats.platform_chat_id, and left it NULL
whenever the chat wasn't in telegram_chats yet -> 101,906 orphaned messages
(invisible in the dashboard, which joins on chat_id).

Every orphaned row carries the raw peer in metadata->'peer_id' (PeerChannel/
PeerChat/PeerUser). telegram_chats stores the RAW id (e.g. '1918992665'), so we:
  1) create a minimal telegram_chats row per distinct orphan peer (~228),
     title left NULL — the collector fills it on next encounter,
  2) relink each orphaned message's chat_id to that chat's UUID.

Idempotent + reversible: ON CONFLICT DO NOTHING on insert; only NULL chat_id
rows are updated. The recurrence fix lives in the collector
(_upsert_message auto-creates the chat when the lookup misses).
"""
import asyncio, asyncpg, os

PCID = ("COALESCE(metadata->'peer_id'->>'channel_id',"
        "         metadata->'peer_id'->>'chat_id',"
        "         metadata->'peer_id'->>'user_id')")


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=180)
    before = await c.fetchval("SELECT count(*) FROM telegram_messages WHERE chat_id IS NULL")
    chats_before = await c.fetchval("SELECT count(*) FROM telegram_chats")

    ins = await c.execute(f"""
        INSERT INTO telegram_chats (platform_chat_id, type)
        SELECT DISTINCT {PCID} AS pcid,
               CASE metadata->'peer_id'->>'_'
                 WHEN 'PeerChannel' THEN 'channel'
                 WHEN 'PeerChat'    THEN 'group'
                 WHEN 'PeerUser'    THEN 'user'
               END AS type
        FROM telegram_messages
        WHERE chat_id IS NULL AND metadata ? 'peer_id' AND {PCID} IS NOT NULL
        ON CONFLICT (platform_chat_id) DO NOTHING
    """)
    print("insert chats:", ins)

    upd = await c.execute(f"""
        UPDATE telegram_messages m
        SET chat_id = tc.id
        FROM telegram_chats tc
        WHERE m.chat_id IS NULL AND m.metadata ? 'peer_id'
          AND tc.platform_chat_id = {PCID}
    """)
    print("relink messages:", upd)

    after = await c.fetchval("SELECT count(*) FROM telegram_messages WHERE chat_id IS NULL")
    chats_after = await c.fetchval("SELECT count(*) FROM telegram_chats")
    print(f"null chat_id {before} -> {after} (relinked {before - after})")
    print(f"telegram_chats {chats_before} -> {chats_after} (created {chats_after - chats_before})")
    await c.close()

asyncio.run(main())
