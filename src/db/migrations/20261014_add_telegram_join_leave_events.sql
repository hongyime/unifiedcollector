CREATE TABLE IF NOT EXISTS telegram_join_leave_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id UUID NOT NULL REFERENCES telegram_chats(id) ON DELETE CASCADE,
    message_id UUID NOT NULL REFERENCES telegram_messages(id) ON DELETE CASCADE,
    platform_chat_id TEXT NOT NULL,
    platform_message_id TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES telegram_users(id) ON DELETE CASCADE,
    actor_user_id UUID REFERENCES telegram_users(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('join', 'leave')),
    action_type TEXT NOT NULL,
    event_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (message_id, user_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_telegram_join_leave_chat_time
    ON telegram_join_leave_events (chat_id, event_at DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_telegram_join_leave_user_time
    ON telegram_join_leave_events (user_id, event_at DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_telegram_join_leave_type_time
    ON telegram_join_leave_events (event_type, event_at DESC NULLS LAST);
