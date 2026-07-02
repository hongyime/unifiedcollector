-- 2026-07-02: partial index for feature B ("what changed since last viewed").
-- Telegram deletion lookups (metadata->>'deleted'='true') were a ~12s seq-scan over
-- 670k+ rows — metadata JSONB isn't indexable by default — which timed out the
-- analyzer's /api/entities/{id}/changelog. Mirrors the existing partial deleted
-- indexes idx_beeper_msgs_deleted / idx_whatsapp_messages_deleted.
--
-- NOTE: created live via CREATE INDEX CONCURRENTLY (no table lock); this persists it
-- across any schema recreate. We use PLAIN CREATE INDEX here (not CONCURRENTLY)
-- because the migrate runner executes each migration inside a transaction and
-- CONCURRENTLY is forbidden in one. IF NOT EXISTS => no-op on the live DB (index
-- already present), and a brief lock on a fresh/empty table during recreate (fine).
CREATE INDEX IF NOT EXISTS idx_tg_messages_deleted_sender
    ON telegram_messages (sender_id)
    WHERE metadata->>'deleted' = 'true';
