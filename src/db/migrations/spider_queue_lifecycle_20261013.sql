-- Extend the shared spider queue with durable retry, provenance, and lease state.
-- Existing source-specific queues remain authoritative until shadow parity has
-- been measured; this migration does not move or delete their rows.

ALTER TABLE spider_queue
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS terminal_reason VARCHAR(128),
    ADD COLUMN IF NOT EXISTS discovery_type VARCHAR(64),
    ADD COLUMN IF NOT EXISTS source_table VARCHAR(128),
    ADD COLUMN IF NOT EXISTS source_record_id VARCHAR(500),
    ADD COLUMN IF NOT EXISTS source_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(600),
    ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 5,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claim_token UUID;

-- Pre-lifecycle rows use their canonical target as the idempotency key.
UPDATE spider_queue
   SET idempotency_key = platform || ':' || node_id
 WHERE idempotency_key IS NULL;

ALTER TABLE spider_queue
    ALTER COLUMN idempotency_key SET NOT NULL;

-- Preserve in-flight work during rollout. Existing claims expire five minutes
-- after their last attempt; rows without a timestamp receive a fresh lease.
UPDATE spider_queue
   SET claimed_at = COALESCE(last_attempted_at, NOW()),
       lease_expires_at = COALESCE(last_attempted_at, NOW()) + INTERVAL '5 minutes'
 WHERE status = 'in_progress'
   AND lease_expires_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_spider_queue_idempotency
    ON spider_queue (platform, idempotency_key);

CREATE INDEX IF NOT EXISTS idx_spider_queue_due
    ON spider_queue (platform, status, next_attempt_at, hop_distance, priority, enqueued_at);

CREATE INDEX IF NOT EXISTS idx_spider_queue_expired_lease
    ON spider_queue (platform, lease_expires_at)
    WHERE status = 'in_progress';

-- A comparison shadow of legacy queue rows allows status parity checks without
-- changing which collector owns or claims the source-specific queue.
CREATE TABLE IF NOT EXISTS spider_queue_legacy_shadow (
    source_table       VARCHAR(128) NOT NULL,
    source_record_id   VARCHAR(500) NOT NULL,
    platform           VARCHAR(50) NOT NULL,
    node_id            VARCHAR(500) NOT NULL,
    legacy_status      VARCHAR(32) NOT NULL,
    status             VARCHAR(32) NOT NULL,
    priority           INTEGER NOT NULL DEFAULT 5,
    attempts           INTEGER NOT NULL DEFAULT 0,
    next_attempt_at    TIMESTAMPTZ,
    parent_node_id     VARCHAR(500),
    hop_distance       INTEGER,
    discovery_type     VARCHAR(64),
    edge_type          VARCHAR(50),
    terminal_reason    VARCHAR(128),
    source_metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    sync_generation    VARCHAR(128) NOT NULL DEFAULT 'legacy',
    retired_at         TIMESTAMPTZ,
    synced_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_table, source_record_id)
);

CREATE INDEX IF NOT EXISTS idx_spider_queue_legacy_shadow_status
    ON spider_queue_legacy_shadow (source_table, sync_generation, status);
