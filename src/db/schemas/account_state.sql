-- Wave 2.4: AccountPool persistence.
--
-- Survives worker restart for:
--   - daily quota counters (profile_views_today, actions_today)
--   - active cooldown deadlines (locked_until_ts + reason)
--   - last_error_kind (so restarted worker doesn't re-route to
--     a known-dead account)
--
-- All statements IF NOT EXISTS safe.

CREATE TABLE IF NOT EXISTS account_state (
    account_name        VARCHAR(255) PRIMARY KEY,
    -- daily quota
    quota_window_start  TIMESTAMPTZ,
    profile_views_today INTEGER NOT NULL DEFAULT 0,
    actions_today       INTEGER NOT NULL DEFAULT 0,
    -- cooldown (epoch wall-time, NOT monotonic, so it survives restart)
    locked_until_wall   TIMESTAMPTZ,
    cooldown_reason     VARCHAR(64) NOT NULL DEFAULT '',
    last_error_kind     VARCHAR(64) NOT NULL DEFAULT '',
    -- counters (informational; not authoritative for routing)
    error_count         INTEGER NOT NULL DEFAULT 0,
    success_count       INTEGER NOT NULL DEFAULT 0,
    total_requests      BIGINT  NOT NULL DEFAULT 0,
    -- audit
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_account_state_locked
    ON account_state(locked_until_wall)
    WHERE locked_until_wall IS NOT NULL;
