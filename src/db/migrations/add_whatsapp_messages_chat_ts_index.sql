-- 2026-07-09: composite (chat_id, timestamp DESC) index on whatsapp_messages
-- so the dashboard's /whatsapp/chats endpoint can serve last-message previews
-- via a per-chat index scan instead of a full 46k-row seq scan + sort.
-- Without this the endpoint takes ~8s (bare timestamp column has no index),
-- with it the DISTINCT ON drops to ~30ms on the same table.
-- Built CONCURRENTLY out-of-band on the live hot DB (whatsapp_messages is
-- append-heavy — realtime bridge writes constantly); plain form here for
-- clean-rebuild parity (IF NOT EXISTS = no-op where already applied).
CREATE INDEX IF NOT EXISTS idx_wa_messages_chat_ts
    ON whatsapp_messages (chat_id, "timestamp" DESC NULLS LAST);
