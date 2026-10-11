-- Fudan pattern: source_controls table for failure taxonomy + stop conditions
-- Tracks collector source health with explicit states and stop reasons.

CREATE TABLE IF NOT EXISTS source_controls (
    source_name TEXT PRIMARY KEY,
    -- State: HEALTHY, RATE_LIMITED, AUTH_FAILED, SOURCE_DEAD, PAUSED
    state TEXT NOT NULL DEFAULT 'HEALTHY'
        CHECK (state IN ('HEALTHY', 'RATE_LIMITED', 'AUTH_FAILED', 'SOURCE_DEAD', 'PAUSED')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_success TIMESTAMPTZ,
    last_attempt TIMESTAMPTZ,
    -- For RATE_LIMITED: when the block expires
    blocked_until TIMESTAMPTZ,
    -- Human-readable reason for non-HEALTHY state
    stop_reason TEXT,
    -- Consecutive failure count (for backoff)
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    -- Who/when disabled (for audit)
    disabled_by TEXT,
    disabled_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_source_controls_state ON source_controls(state);
CREATE INDEX IF NOT EXISTS idx_source_controls_enabled ON source_controls(enabled);

COMMENT ON TABLE source_controls IS 'Fudan-pattern source health tracking: explicit states, stop reasons, no infinite retries';
