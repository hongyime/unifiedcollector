-- Preserve typed source evidence for platform-specific discovery adapters.
-- Shared spider queue rows remain gated and legacy workers remain authoritative.

CREATE TABLE IF NOT EXISTS spider_discovery_evidence (
    id BIGSERIAL PRIMARY KEY,
    evidence_key CHAR(64) NOT NULL UNIQUE,
    platform VARCHAR(50) NOT NULL,
    target_type VARCHAR(32) NOT NULL CHECK (
        target_type IN ('account', 'channel', 'chat', 'member', 'link')
    ),
    target_node_id VARCHAR(500) NOT NULL,
    discovery_type VARCHAR(64) NOT NULL,
    source_table VARCHAR(128) NOT NULL,
    source_record_id VARCHAR(500) NOT NULL,
    confidence NUMERIC(4, 3) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    source_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_spider_discovery_evidence_target
    ON spider_discovery_evidence (platform, target_type, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_spider_discovery_evidence_source
    ON spider_discovery_evidence (platform, source_table, last_seen_at DESC);
