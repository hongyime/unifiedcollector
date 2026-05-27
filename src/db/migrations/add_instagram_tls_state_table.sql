-- Migration: per-account TLS fingerprint pinning state for Instagram.
-- Created for src/core/tls_fingerprint.py (TLSFingerprintRotator).
--
-- One row per Instagram account name. Tracks which curl_cffi impersonate
-- target it is pinned to, plus rotation bookkeeping. Rotation is driven
-- by 403 / 429 failures and gated by a cooldown window in code; this
-- table is just the durable state.
--
-- This migration is idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS instagram_tls_state (
    account_id          VARCHAR(128) PRIMARY KEY,
    impersonate_target  VARCHAR(64)  NOT NULL,
    last_rotation_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    rotation_count      INT          NOT NULL DEFAULT 0,
    last_failure_reason TEXT,
    CONSTRAINT instagram_tls_state_rot_chk CHECK (rotation_count >= 0)
);

-- Reporting: scan all accounts ordered by churn.
CREATE INDEX IF NOT EXISTS idx_instagram_tls_state_rotation
    ON instagram_tls_state (rotation_count DESC, last_rotation_at DESC);

-- ---------------------------------------------------------------------------
-- DOWN:
-- DROP INDEX IF EXISTS idx_instagram_tls_state_rotation;
-- DROP TABLE IF EXISTS instagram_tls_state;
