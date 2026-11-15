CREATE TABLE IF NOT EXISTS source_recovery_profiles (
    source_name TEXT NOT NULL,
    account_identity TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK (state IN ('restricted', 'normal')),
    reason TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    restricted_until TIMESTAMPTZ NOT NULL,
    stale_since TIMESTAMPTZ,
    last_observation_at TIMESTAMPTZ,
    clean_passes INTEGER NOT NULL DEFAULT 0,
    restriction_count INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_name, account_identity)
);

CREATE INDEX IF NOT EXISTS idx_source_recovery_profiles_active
    ON source_recovery_profiles (source_name, restricted_until DESC);
