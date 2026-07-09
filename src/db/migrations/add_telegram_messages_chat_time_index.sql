-- 2026-07-09: composite (chat_id, platform_created_at DESC NULLS LAST) index
-- on telegram_messages so the dashboard's /telegram/chat/{id} endpoint can
-- pick the newest N messages via an index range scan instead of a full
-- per-chat bitmap heap scan + top-N sort.
--
-- Without this the endpoint was 25s cold-cache / 4.5s warm on the busiest
-- chats (63k msgs each) — Postgres had to read 13k+ heap pages just to
-- decide which 200 rows to keep. Even mid-sized chats (~500 msgs) hit ~10s
-- cold because chat_id-only bitmap scans touched ~500 heap pages before the
-- sort could pick winners. With this index the same query is bounded by
-- the LIMIT (index descent + 200 tuples, sub-100ms warm, ~1s cold).
--
-- Built CONCURRENTLY out-of-band on the live hot DB (telegram_messages is
-- append-heavy — realtime MTProto writes constantly and backfill runs to
-- 2018); plain form here for clean-rebuild parity, mirroring the sibling
-- add_whatsapp_messages_chat_ts_index.sql. IF NOT EXISTS => no-op on the
-- live DB (index already present), brief lock on a fresh/empty table
-- during recreate (fine).
CREATE INDEX IF NOT EXISTS idx_tg_messages_chat_time
    ON telegram_messages (chat_id, platform_created_at DESC NULLS LAST);
