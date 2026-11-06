CREATE TABLE IF NOT EXISTS control_bot_updates (
    update_id BIGINT PRIMARY KEY,
    payload JSONB NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_bot_cards (
    token TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id BIGINT,
    expires_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    resolution TEXT,
    resolved_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS control_bot_cards_pending_idx
    ON control_bot_cards (expires_at) WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS control_bot_operations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    card_token TEXT REFERENCES control_bot_cards(token),
    action TEXT NOT NULL CHECK (action IN ('restart', 'pause', 'resume', 'ignore')),
    target TEXT,
    actor_id BIGINT NOT NULL,
    actor_username TEXT,
    signature TEXT,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'rejected')),
    result JSONB,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS control_bot_operations_pending_idx
    ON control_bot_operations (created_at) WHERE status = 'queued';

CREATE TABLE IF NOT EXISTS control_bot_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event TEXT NOT NULL,
    actor_id BIGINT,
    card_token TEXT,
    operation_id BIGINT,
    detail JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_bot_outbox (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operation_id BIGINT NOT NULL UNIQUE REFERENCES control_bot_operations(id),
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS control_bot_transport_health (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    last_poll_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
