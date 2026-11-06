CREATE TABLE IF NOT EXISTS memory_recovery_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    pressure_since TIMESTAMPTZ,
    recovery_since TIMESTAMPTZ,
    paused_target TEXT,
    cooldown_until TIMESTAMPTZ,
    circuit_open_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memory_recovery_actions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    target TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('pause', 'resume')),
    status TEXT NOT NULL CHECK (status IN ('awaiting_cancel', 'queued', 'succeeded', 'failed', 'cancelled')),
    reason TEXT NOT NULL,
    metrics JSONB NOT NULL,
    card_token TEXT UNIQUE REFERENCES control_bot_cards(token),
    operation_id BIGINT UNIQUE REFERENCES control_bot_operations(id),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_memory_recovery_actions_pending
    ON memory_recovery_actions (status, created_at) WHERE status IN ('awaiting_cancel', 'queued');
