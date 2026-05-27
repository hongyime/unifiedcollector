-- Migration: add telegram_chat_members table
-- Wave 2 Batch E: Telegram common-chat-membership for daily 03:00 SGT refresh.
-- Tracks which Telegram users are members of which chats, refreshed daily by
-- the unified collector cron via TelegramCollector.collect_chat_members(chat_id).
--
-- Idempotent — safe to run repeatedly.
--
-- chat_id, user_id are Telegram-native int64 IDs (not unified UUIDs); this
-- table is operationally adjacent to telegram_chats / telegram_users but
-- intentionally keyed off the platform IDs so the daily refresh job does
-- not need to resolve UUID lookups for every participant.

CREATE TABLE IF NOT EXISTS telegram_chat_members (
    chat_id          BIGINT      NOT NULL,
    user_id          BIGINT      NOT NULL,
    role             VARCHAR(32),
    joined_at        TIMESTAMPTZ,
    last_seen_at     TIMESTAMPTZ,
    refreshed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chat_id, user_id)
);

-- Reverse lookup: "which chats is this user in?" — used by spider /
-- common-chat discovery cross-referencing. Without this index every
-- such query would seq-scan the whole table.
CREATE INDEX IF NOT EXISTS idx_telegram_chat_members_user
    ON telegram_chat_members (user_id);

-- Stale-row pruning: cron job pulls (refreshed_at < NOW() - 7d) to drop
-- members that haven't been re-observed for a week.
CREATE INDEX IF NOT EXISTS idx_telegram_chat_members_refreshed
    ON telegram_chat_members (refreshed_at);

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_telegram_chat_members_refreshed;
-- DROP INDEX IF EXISTS idx_telegram_chat_members_user;
-- DROP TABLE IF EXISTS telegram_chat_members;
