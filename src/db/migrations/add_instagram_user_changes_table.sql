-- Migration: add instagram_user_changes table
-- Per-user profile-field diffs detected during Instagram ingestion.
-- Mirrors telegram_user_changes shape — backed by the same generic
-- src/core/user_change_tracker.UserChangeTracker module.
--
-- user_id is BIGINT to match Instagram's numeric pk
-- (instagram_profiles.platform_user_id stores the same id as a string).
--
-- Idempotent — safe to run repeatedly.

CREATE TABLE IF NOT EXISTS instagram_user_changes (
    id           BIGSERIAL    PRIMARY KEY,
    user_id      BIGINT       NOT NULL,
    field        VARCHAR(64)  NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    detected_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_instagram_user_changes_user
    ON instagram_user_changes (user_id, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_instagram_user_changes_field
    ON instagram_user_changes (field, detected_at DESC);

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_instagram_user_changes_field;
-- DROP INDEX IF EXISTS idx_instagram_user_changes_user;
-- DROP TABLE IF EXISTS instagram_user_changes;
