-- Reconciler state (feature_gap_analysis #5). Idempotent; the reconciler also
-- creates these on first DB touch, this is for clean-boot consistency.
-- recover_state: per-source "refill complete" marker (drops source to fast mode).
-- media_recover_state: per-item re-download attempt count + tombstone (an item
-- that fails tombstone_after times is permanently skipped, stopping the
-- forever-retry of expired/lost source assets).
CREATE TABLE IF NOT EXISTS recover_state (
    source     TEXT PRIMARY KEY,
    done_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS media_recover_state (
    source          TEXT NOT NULL,
    content_id      TEXT NOT NULL,
    attempts        INT NOT NULL DEFAULT 0,
    tombstoned_at   TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, content_id)
);

CREATE INDEX IF NOT EXISTS idx_media_recover_tombstoned
    ON media_recover_state (source) WHERE tombstoned_at IS NOT NULL;
