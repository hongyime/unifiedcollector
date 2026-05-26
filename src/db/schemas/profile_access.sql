-- Wave 2.3: ProfileAccessTracker (cross-source).
--
-- Tracks WHICH of our accounts can access WHICH targets, so that on
-- the next pass we route a target to the account most likely to
-- actually retrieve data — instead of randomly picking and burning
-- request budget on rejected calls.
--
-- Source-agnostic: works for Instagram (private profiles), TikTok
-- (account-locked content), Lemon8, etc. The (source, target_id)
-- composite key isolates per-source namespaces.
--
-- All statements are IF NOT EXISTS safe — re-running init_db() is
-- idempotent.

CREATE TABLE IF NOT EXISTS profile_access_attempts (
    id              BIGSERIAL PRIMARY KEY,
    source          VARCHAR(20)  NOT NULL,
    target_id       VARCHAR(255) NOT NULL,
    accessing_account VARCHAR(255) NOT NULL,
    can_access      BOOLEAN      NOT NULL,
    is_public       BOOLEAN,
    is_followed     BOOLEAN      NOT NULL DEFAULT FALSE,
    error_msg       TEXT,
    attempt_ts      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paa_target
    ON profile_access_attempts(source, target_id);

CREATE INDEX IF NOT EXISTS idx_paa_target_success
    ON profile_access_attempts(source, target_id, attempt_ts DESC)
    WHERE can_access = TRUE;

CREATE INDEX IF NOT EXISTS idx_paa_attempt_ts
    ON profile_access_attempts(attempt_ts);

CREATE TABLE IF NOT EXISTS profile_access_summary (
    source           VARCHAR(20)  NOT NULL,
    target_id        VARCHAR(255) NOT NULL,
    is_public        BOOLEAN,
    last_checked_ts  TIMESTAMPTZ,
    last_success_ts  TIMESTAMPTZ,
    total_attempts   INTEGER      NOT NULL DEFAULT 0,
    accessible_by    JSONB        NOT NULL DEFAULT '[]'::JSONB,
    PRIMARY KEY (source, target_id)
);

CREATE INDEX IF NOT EXISTS idx_pas_last_checked
    ON profile_access_summary(last_checked_ts);
