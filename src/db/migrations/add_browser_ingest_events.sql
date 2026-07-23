CREATE TABLE IF NOT EXISTS browser_ingest_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    subject TEXT,
    observed_count INTEGER NOT NULL DEFAULT 0,
    stored_count INTEGER NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_browser_ingest_events_created
    ON browser_ingest_events (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_browser_ingest_events_platform_endpoint
    ON browser_ingest_events (platform, endpoint, created_at DESC);
