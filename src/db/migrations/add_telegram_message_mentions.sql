-- Normalize Telegram @username / inline-user mentions as message-level evidence.

CREATE TABLE IF NOT EXISTS telegram_message_mentions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id          UUID NOT NULL REFERENCES telegram_messages(id) ON DELETE CASCADE,
    mentioned_user_id   UUID REFERENCES telegram_users(id) ON DELETE SET NULL,
    mention_username    TEXT,
    mention_source      TEXT NOT NULL,
    offset_start        INTEGER,
    length              INTEGER,
    raw_text            TEXT,
    refreshed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_message_mentions_unique
    ON telegram_message_mentions (
        message_id,
        COALESCE(mentioned_user_id::text, ''),
        COALESCE(lower(mention_username), ''),
        COALESCE(offset_start, -1),
        COALESCE(length, -1)
    );

CREATE INDEX IF NOT EXISTS idx_telegram_message_mentions_message
    ON telegram_message_mentions (message_id);

CREATE INDEX IF NOT EXISTS idx_telegram_message_mentions_user
    ON telegram_message_mentions (mentioned_user_id);

CREATE INDEX IF NOT EXISTS idx_telegram_message_mentions_username
    ON telegram_message_mentions (lower(mention_username));
