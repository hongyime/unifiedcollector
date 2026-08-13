-- Domain-aware website/search crawl pacing telemetry.
-- Records bounded summary/exception events, not every request.

CREATE TABLE IF NOT EXISTS collector_domain_pacing_events (
    id                  BIGSERIAL PRIMARY KEY,
    source              TEXT NOT NULL,
    registrable_domain  TEXT NOT NULL,
    host                TEXT,
    event_type          TEXT NOT NULL,
    url                 TEXT,
    status_code         INTEGER,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_collector_domain_pacing_source_created
    ON collector_domain_pacing_events (source, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_collector_domain_pacing_domain_created
    ON collector_domain_pacing_events (source, registrable_domain, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_collector_domain_pacing_event_created
    ON collector_domain_pacing_events (event_type, created_at DESC);
