CREATE TABLE IF NOT EXISTS collector_seen_targets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_key TEXT NOT NULL,
    target_display TEXT,
    origin TEXT NOT NULL DEFAULT 'collector',
    priority INTEGER NOT NULL DEFAULT 5,
    evidence_count BIGINT NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_backfill_at TIMESTAMPTZ,
    next_backfill_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'pending',
    source_table TEXT,
    source_record_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT collector_seen_targets_key UNIQUE (platform, target_type, target_key),
    CONSTRAINT collector_seen_targets_status_chk CHECK (
        status IN ('seen', 'new', 'pending', 'backfilled', 'fresh', 'stale', 'failed', 'skipped')
    )
);

CREATE INDEX IF NOT EXISTS idx_collector_seen_targets_platform_status
    ON collector_seen_targets (platform, status, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_collector_seen_targets_type_status
    ON collector_seen_targets (target_type, status, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_collector_seen_targets_next_backfill
    ON collector_seen_targets (next_backfill_at)
    WHERE next_backfill_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_collector_seen_targets_newly_seen
    ON collector_seen_targets (first_seen_at DESC);

ALTER TABLE collection_coverage_snapshots
    ADD COLUMN IF NOT EXISTS seen_targets_total BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS seen_targets_backfilled BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS seen_targets_pending BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS seen_targets_fresh BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS seen_targets_stale BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS seen_targets_newly_discovered BIGINT NOT NULL DEFAULT 0;
