CREATE TABLE IF NOT EXISTS collector_operational_events (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',
    summary     TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_collector_operational_events_created
    ON collector_operational_events (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_collector_operational_events_source_created
    ON collector_operational_events (source, created_at DESC);
