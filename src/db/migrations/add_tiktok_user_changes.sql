-- Migration: add tiktok_user_changes table
-- Per-user profile-field diffs detected during TikTok ingestion.
-- Mirrors telegram_user_changes shape — backed by the same generic
-- src/core/user_change_tracker.UserChangeTracker module, whose INSERT writes
-- exactly (user_id, field, old_value, new_value); detected_at is DB-defaulted.
--
-- user_id is VARCHAR(255) because tiktok_profiles.platform_user_id is stored
-- as a string (TikTok numeric ids exceed int32 and are handled as text).
--
-- Idempotent — safe to run repeatedly.

CREATE TABLE IF NOT EXISTS tiktok_user_changes (
    id           BIGSERIAL    PRIMARY KEY,
    user_id      VARCHAR(255) NOT NULL,
    field        VARCHAR(64)  NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    detected_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tiktok_user_changes_user
    ON tiktok_user_changes (user_id, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_tiktok_user_changes_field
    ON tiktok_user_changes (field, detected_at DESC);

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_tiktok_user_changes_field;
-- DROP INDEX IF EXISTS idx_tiktok_user_changes_user;
-- DROP TABLE IF EXISTS tiktok_user_changes;
