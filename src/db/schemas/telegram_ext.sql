-- Telegram extended schema: admin log events, group join queue

CREATE TABLE IF NOT EXISTS telegram_admin_events (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    actor_id BIGINT,
    target_id BIGINT,
    detail JSONB,
    event_id BIGINT,
    occurred_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tae_chat ON telegram_admin_events(chat_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_tae_type ON telegram_admin_events(event_type);
CREATE INDEX IF NOT EXISTS idx_tae_event_id ON telegram_admin_events(chat_id, event_id);

CREATE TABLE IF NOT EXISTS telegram_group_joins (
    id SERIAL PRIMARY KEY,
    link TEXT NOT NULL,
    link_type VARCHAR(20) NOT NULL DEFAULT 'invite',
    chat_title VARCHAR(255),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    account_name VARCHAR(100),
    error TEXT,
    queued_at TIMESTAMP NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tgj_status ON telegram_group_joins(status);
