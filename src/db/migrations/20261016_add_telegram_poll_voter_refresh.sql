ALTER TABLE telegram_polls
    ADD COLUMN IF NOT EXISTS voters_checked_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_telegram_polls_voter_refresh
    ON telegram_polls(voters_checked_at ASC NULLS FIRST, refreshed_at ASC)
    WHERE is_anonymous = FALSE;
