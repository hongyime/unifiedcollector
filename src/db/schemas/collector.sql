-- Core schema shared by all collectors.
-- Run on every startup via init_db() — all statements are IF NOT EXISTS safe.

CREATE TABLE IF NOT EXISTS media_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source VARCHAR(20) NOT NULL,
    entity_id VARCHAR(100) NOT NULL,
    entity_name VARCHAR(255) NOT NULL,
    content_type VARCHAR(50) NOT NULL,
    content_id VARCHAR(100) NOT NULL,
    filename VARCHAR(500) NOT NULL,
    file_path VARCHAR(1000) NOT NULL,
    file_size BIGINT,
    width INTEGER,
    height INTEGER,
    sha256 VARCHAR(64),
    collected_at TIMESTAMP NOT NULL DEFAULT NOW(),
    source_url TEXT,
    metadata JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_media_source ON media_items(source);
CREATE INDEX IF NOT EXISTS idx_media_entity ON media_items(source, entity_id);
CREATE INDEX IF NOT EXISTS idx_media_collected ON media_items(collected_at);
CREATE INDEX IF NOT EXISTS idx_media_sha256 ON media_items(source, sha256);
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_source_content ON media_items(source, content_id);

CREATE TABLE IF NOT EXISTS service_cursors (
    service VARCHAR(50) PRIMARY KEY,
    last_processed_id VARCHAR(100),
    last_processed_at TIMESTAMP,
    status VARCHAR(20) NOT NULL DEFAULT 'idle'
);

CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id SERIAL PRIMARY KEY,
    source VARCHAR(20) NOT NULL,
    entity_id VARCHAR(100),
    content_id VARCHAR(100),
    error_message TEXT,
    retry_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Wave 2.5: non-destructive columns for retry scheduling.
-- ADD COLUMN IF NOT EXISTS is idempotent on every restart.
ALTER TABLE dead_letter_queue
    ADD COLUMN IF NOT EXISTS next_retry_at  TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE dead_letter_queue
    ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ;
ALTER TABLE dead_letter_queue
    ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'pending';
    -- status: 'pending' | 'in_progress' | 'failed' | 'succeeded'
    -- Rows in 'succeeded' are typically deleted; kept here as escape
    -- hatch when a handler wants to keep the audit trail.

CREATE INDEX IF NOT EXISTS idx_dlq_source ON dead_letter_queue(source, created_at);
CREATE INDEX IF NOT EXISTS idx_dlq_due
    ON dead_letter_queue(next_retry_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_dlq_status
    ON dead_letter_queue(status, source);

CREATE TABLE IF NOT EXISTS collection_targets (
    id SERIAL PRIMARY KEY,
    source VARCHAR(20) NOT NULL,
    target_id VARCHAR(100) NOT NULL,
    target_name VARCHAR(255),
    target_type VARCHAR(50) DEFAULT 'user',
    status VARCHAR(20) DEFAULT 'pending',
    priority INT DEFAULT 0,
    last_collection_at TIMESTAMP,
    collection_count INT DEFAULT 0,
    error_message TEXT,
    metadata JSONB,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(source, target_id)
);

CREATE INDEX IF NOT EXISTS idx_targets_source_status
    ON collection_targets(source, status);
CREATE INDEX IF NOT EXISTS idx_targets_priority
    ON collection_targets(priority DESC);

CREATE TABLE IF NOT EXISTS account_proximity_cache (
    platform VARCHAR(30) NOT NULL,
    account_id VARCHAR(255) NOT NULL,
    owner_account VARCHAR(255) NOT NULL,
    tier SMALLINT NOT NULL CHECK (tier BETWEEN 1 AND 4),
    reasons JSONB NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (platform, account_id, owner_account)
);
CREATE INDEX IF NOT EXISTS idx_account_proximity_cache_lookup
    ON account_proximity_cache(platform, account_id, tier);
CREATE INDEX IF NOT EXISTS idx_account_proximity_cache_tier
    ON account_proximity_cache(tier);

CREATE TABLE IF NOT EXISTS collection_runs (
    id SERIAL PRIMARY KEY,
    source VARCHAR(20) NOT NULL,
    status VARCHAR(20) DEFAULT 'queued',
    started_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP,
    items_collected INT DEFAULT 0,
    items_failed INT DEFAULT 0,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_source ON collection_runs(source);
CREATE INDEX IF NOT EXISTS idx_runs_status ON collection_runs(status);

CREATE TABLE IF NOT EXISTS discovered_links (
    id               SERIAL PRIMARY KEY,
    source           VARCHAR(50) NOT NULL,
    source_table     VARCHAR(100),
    source_record_id TEXT NOT NULL,
    context_id       TEXT,
    entity_id        TEXT,
    url              TEXT NOT NULL,
    domain           TEXT,
    link_type        TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    title            TEXT,
    description      TEXT,
    discovered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fetched_at       TIMESTAMPTZ,
    metadata         JSONB,
    UNIQUE (source, source_record_id, url)
);

CREATE INDEX IF NOT EXISTS idx_discovered_links_source
    ON discovered_links (source, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovered_links_domain
    ON discovered_links (domain);
CREATE INDEX IF NOT EXISTS idx_discovered_links_status
    ON discovered_links (status, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovered_links_context
    ON discovered_links (source, context_id);

CREATE TABLE IF NOT EXISTS collection_schedules (
    id SERIAL PRIMARY KEY,
    source VARCHAR(20) UNIQUE NOT NULL,
    interval_hours INT NOT NULL DEFAULT 24,
    enabled BOOLEAN DEFAULT true,
    last_run TIMESTAMP,
    next_run TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_schedules_next_run
    ON collection_schedules(next_run) WHERE enabled = true;

CREATE TABLE IF NOT EXISTS dashboard_users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'viewer',
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
