-- Operator-visible API quota snapshots for GitHub and YouTube.

CREATE TABLE IF NOT EXISTS collector_api_quota_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    service         TEXT NOT NULL,
    account         TEXT NOT NULL,
    bucket          TEXT NOT NULL,
    quota_date      DATE NOT NULL,
    reset_at        TIMESTAMPTZ,
    used_units      INTEGER NOT NULL DEFAULT 0,
    remaining_units INTEGER,
    quota_units     INTEGER NOT NULL DEFAULT 0,
    target_units    INTEGER NOT NULL DEFAULT 0,
    target_ratio    NUMERIC(5,4) NOT NULL DEFAULT 0.9000,
    paused          BOOLEAN NOT NULL DEFAULT FALSE,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (service, account, bucket, quota_date)
);

CREATE INDEX IF NOT EXISTS idx_collector_api_quota_service_updated
    ON collector_api_quota_snapshots (service, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_collector_api_quota_service_bucket
    ON collector_api_quota_snapshots (service, bucket, quota_date DESC);
