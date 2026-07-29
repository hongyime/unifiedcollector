-- Store Telegram admin-log events when an account has permission to read them.

CREATE TABLE IF NOT EXISTS telegram_admin_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id             UUID NOT NULL REFERENCES telegram_chats(id) ON DELETE CASCADE,
    platform_event_id   TEXT NOT NULL,
    actor_user_id       UUID REFERENCES telegram_users(id) ON DELETE SET NULL,
    target_user_id      UUID REFERENCES telegram_users(id) ON DELETE SET NULL,
    action_type         TEXT NOT NULL,
    event_at            TIMESTAMPTZ,
    message_platform_id TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    collected_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chat_id, platform_event_id)
);

CREATE INDEX IF NOT EXISTS idx_telegram_admin_events_chat_time
    ON telegram_admin_events (chat_id, event_at DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_telegram_admin_events_actor
    ON telegram_admin_events (actor_user_id);

CREATE INDEX IF NOT EXISTS idx_telegram_admin_events_target
    ON telegram_admin_events (target_user_id);

CREATE INDEX IF NOT EXISTS idx_telegram_admin_events_action
    ON telegram_admin_events (action_type);
