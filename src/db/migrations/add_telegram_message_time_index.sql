-- 2026-07-20: dashboard messaging coverage needs the latest native Telegram
-- message timestamp. Without a standalone platform_created_at index, Postgres
-- scans/sorts the full telegram_messages table.
--
-- Built CONCURRENTLY out-of-band on the live DB; plain form here because the
-- migration runner wraps migrations in a transaction.
CREATE INDEX IF NOT EXISTS idx_tg_messages_platform_created_at
    ON telegram_messages (platform_created_at DESC NULLS LAST);
