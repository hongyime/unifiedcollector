-- 2026-10-29: source_controls table (Fudan failure taxonomy) — remount.
--
-- The original 20261012_add_source_controls.sql was deleted after its
-- COMMENT ON TABLE statement sent the migration runner into a
-- permission-denied boot loop, but the production table already exists
-- (0 rows, applied before deletion). This file uses a NEW dated name — the
-- production schema_migrations ledger may still record the deleted filename
-- and the checksum verifier rejects re-applying an edited file — and is
-- fully idempotent so it is also safe to run by hand: every statement is
-- IF NOT EXISTS-flavoured, ADD COLUMN IF NOT EXISTS heals any drift on the
-- orphan table, and there is deliberately NO COMMENT ON statement.

CREATE TABLE IF NOT EXISTS source_controls (
    source_name TEXT PRIMARY KEY,
    -- HEALTHY, RATE_LIMITED, AUTH_FAILED, SOURCE_DEAD, PAUSED
    state TEXT NOT NULL DEFAULT 'HEALTHY'
        CHECK (state IN ('HEALTHY', 'RATE_LIMITED', 'AUTH_FAILED', 'SOURCE_DEAD', 'PAUSED')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_success TIMESTAMPTZ,
    last_attempt TIMESTAMPTZ,
    -- For RATE_LIMITED: when the block expires
    blocked_until TIMESTAMPTZ,
    -- Human-readable reason for non-HEALTHY state
    stop_reason TEXT,
    -- Consecutive failure count (for backoff); reset to 0 on HEALTHY
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Drift guard for the pre-existing production table: guarantee every column
-- the /api/source-controls router SELECTs exists even if the orphan table was
-- created with a different shape.
ALTER TABLE source_controls
    ADD COLUMN IF NOT EXISTS source_name TEXT,
    ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'HEALTHY',
    ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS last_success TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_attempt TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS blocked_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS stop_reason TEXT,
    ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS idx_source_controls_source_name ON source_controls(source_name);

CREATE INDEX IF NOT EXISTS idx_source_controls_state ON source_controls(state);
CREATE INDEX IF NOT EXISTS idx_source_controls_enabled ON source_controls(enabled);
