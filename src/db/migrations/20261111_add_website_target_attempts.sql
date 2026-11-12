CREATE TABLE IF NOT EXISTS website_target_attempts (
    target_id TEXT PRIMARY KEY,
    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    failure_class TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    http_outcome INTEGER,
    last_successful_page TEXT,
    last_artifact_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_website_target_attempts_retry
    ON website_target_attempts (next_retry_at)
    WHERE next_retry_at IS NOT NULL;
