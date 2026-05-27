-- Migration: add lemon8_user_changes table
-- Per-user profile-field diffs detected during Lemon8 ingestion.
-- Mirrors telegram_user_changes shape — backed by the same generic
-- src/core/user_change_tracker.UserChangeTracker module.
--
-- user_id is VARCHAR(255) because lemon8 platform_user_id is opaque/string-shaped
-- (sometimes a numeric id, sometimes the username when the marker is absent).
--
-- Idempotent — safe to run repeatedly.

CREATE TABLE IF NOT EXISTS lemon8_user_changes (
    id           BIGSERIAL    PRIMARY KEY,
    user_id      VARCHAR(255) NOT NULL,
    field        VARCHAR(64)  NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    detected_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lemon8_user_changes_user
    ON lemon8_user_changes (user_id, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_lemon8_user_changes_field
    ON lemon8_user_changes (field, detected_at DESC);

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_lemon8_user_changes_field;
-- DROP INDEX IF EXISTS idx_lemon8_user_changes_user;
-- DROP TABLE IF EXISTS lemon8_user_changes;
