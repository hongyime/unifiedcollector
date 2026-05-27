-- Migration: add matrix_sync_state table
-- Created for src/core/matrix_client.py (Wave 0 cross-cutting module)
--
-- Persists per-user Matrix /sync state so BeeperMatrixClient can resume
-- across restarts. We use the single-row pattern (PK=user_id) because we
-- have one Beeper account per deployment; if that ever changes the
-- composite key already supports multi-account out of the box.
--
-- Idempotent — safe to run repeatedly.
--
-- Columns:
--   user_id       Matrix MXID, e.g. '@bryan:beeper.com' (also serves as PK)
--   next_batch    opaque token returned by /sync, fed back as `since=`
--   last_sync_at  wall-clock of the last successful sync (monitoring)
--   created_at    bookkeeping

CREATE TABLE IF NOT EXISTS matrix_sync_state (
    user_id        VARCHAR(255) PRIMARY KEY,
    next_batch     TEXT,
    last_sync_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Reverse-lookup of stalled clients (e.g. last_sync_at older than 5 min).
CREATE INDEX IF NOT EXISTS idx_matrix_sync_state_last_sync_at
    ON matrix_sync_state (last_sync_at);

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_matrix_sync_state_last_sync_at;
-- DROP TABLE IF EXISTS matrix_sync_state;
