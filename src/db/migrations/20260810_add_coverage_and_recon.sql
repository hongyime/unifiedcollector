CREATE TABLE IF NOT EXISTS collection_coverage_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL,
    expected_cadence INTERVAL,
    latest_data_at TIMESTAMPTZ,
    latest_run_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'unknown',
    rows_24h BIGINT NOT NULL DEFAULT 0,
    media_24h BIGINT NOT NULL DEFAULT 0,
    errors_24h BIGINT NOT NULL DEFAULT 0,
    rate_limits_24h BIGINT NOT NULL DEFAULT 0,
    private_access_failures BIGINT NOT NULL DEFAULT 0,
    stale_targets JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_collection_coverage_source_created
    ON collection_coverage_snapshots (source, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_collection_coverage_status_created
    ON collection_coverage_snapshots (status, created_at DESC);

CREATE TABLE IF NOT EXISTS recon_targets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_type TEXT NOT NULL,
    target_value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    priority INTEGER NOT NULL DEFAULT 5,
    status TEXT NOT NULL DEFAULT 'pending',
    scope_json JSONB NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (target_type, target_value)
);
CREATE INDEX IF NOT EXISTS idx_recon_targets_pull
    ON recon_targets (status, priority, created_at);

CREATE TABLE IF NOT EXISTS recon_observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_id UUID NOT NULL REFERENCES recon_targets(id) ON DELETE CASCADE,
    module TEXT NOT NULL,
    observation_type TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.25,
    raw_json JSONB NOT NULL DEFAULT '{}',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (target_id, module, observation_type, value)
);
CREATE INDEX IF NOT EXISTS idx_recon_observations_target
    ON recon_observations (target_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_recon_observations_type_value
    ON recon_observations (observation_type, value);

CREATE TABLE IF NOT EXISTS recon_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_observation UUID NOT NULL REFERENCES recon_observations(id) ON DELETE CASCADE,
    to_observation UUID NOT NULL REFERENCES recon_observations(id) ON DELETE CASCADE,
    edge_type TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.25,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (from_observation, to_observation, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_recon_edges_from
    ON recon_edges (from_observation);
CREATE INDEX IF NOT EXISTS idx_recon_edges_to
    ON recon_edges (to_observation);
