CREATE TABLE IF NOT EXISTS collection_action_queue (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    action_type TEXT NOT NULL,
    scope_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    priority INTEGER NOT NULL DEFAULT 5,
    reason TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    generated_by TEXT NOT NULL DEFAULT 'source_matrix',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_collection_action_queue_open_key
    ON collection_action_queue (source, action_type, scope_key)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_collection_action_queue_status_priority
    ON collection_action_queue (status, priority, last_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_collection_action_queue_source_status
    ON collection_action_queue (source, status, last_seen_at DESC);
