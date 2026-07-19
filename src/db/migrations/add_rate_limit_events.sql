-- Durable rate-limit telemetry for dashboard/operator visibility.
CREATE TABLE IF NOT EXISTS rate_limit_events (
    id               BIGSERIAL PRIMARY KEY,
    source           TEXT NOT NULL,
    account          TEXT,
    scope            TEXT,
    status_code      INTEGER,
    cooldown_seconds INTEGER,
    reason           TEXT,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_events_created
    ON rate_limit_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rate_limit_events_source_created
    ON rate_limit_events (source, created_at DESC);
