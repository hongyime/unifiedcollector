-- Typed graph evidence and last-known fetch outcome for the Spider adapters.
-- New writers remain behind per-platform rollout flags until Phase 1 parity.

CREATE TABLE IF NOT EXISTS spider_graph_edges (
    platform VARCHAR(50) NOT NULL,
    source_node_id VARCHAR(500) NOT NULL,
    target_node_id VARCHAR(500) NOT NULL,
    edge_type VARCHAR(64) NOT NULL,
    discovery_type VARCHAR(64) NOT NULL DEFAULT 'graph_edge',
    hop_distance INTEGER NOT NULL DEFAULT 0 CHECK (hop_distance >= 0),
    source_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (platform, source_node_id, target_node_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_spider_graph_edges_target
    ON spider_graph_edges (platform, target_node_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_spider_graph_edges_last_seen
    ON spider_graph_edges (platform, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS spider_fetch_observations (
    platform VARCHAR(50) NOT NULL,
    node_id VARCHAR(500) NOT NULL,
    edge_type VARCHAR(64) NOT NULL,
    outcome VARCHAR(16) NOT NULL CHECK (outcome IN ('success', 'empty', 'unavailable', 'error')),
    edge_count INTEGER NOT NULL DEFAULT 0 CHECK (edge_count >= 0),
    error_class VARCHAR(64),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_success_at TIMESTAMPTZ,
    PRIMARY KEY (platform, node_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_spider_fetch_observations_freshness
    ON spider_fetch_observations (platform, outcome, last_attempt_at DESC);
