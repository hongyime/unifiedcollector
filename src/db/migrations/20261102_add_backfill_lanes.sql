-- Additive common contract; source-native adapters opt in one lane at a time.
CREATE TABLE IF NOT EXISTS backfill_lanes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL,
    account_identity TEXT NOT NULL,
    target_identity TEXT NOT NULL,
    lane TEXT NOT NULL CHECK (lane IN ('realtime', 'backfill')),
    cursor JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','leased','paused','complete','failed','cooldown_deferred')),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    fence_token BIGINT NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    cooldown_until TIMESTAMPTZ,
    item_cost BIGINT NOT NULL DEFAULT 0,
    byte_cost BIGINT NOT NULL DEFAULT 0,
    latest_durable_write_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, account_identity, target_identity, lane)
);
CREATE INDEX IF NOT EXISTS idx_backfill_lanes_claim
    ON backfill_lanes (lane, status, next_retry_at, lease_expires_at);
