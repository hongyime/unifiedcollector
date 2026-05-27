-- Migration: add telegram_user_changes table
-- Wave 2 residual port: per-user profile-field diffs detected during ingestion.
-- Logs change events (username, first_name, last_name, bio, profile_photo_id, etc.)
-- when a Telegram user is re-observed with different values from the last known state.
--
-- Generic enough that the same UserChangeTracker module backing this table can be
-- pointed at instagram_user_changes / lemon8_user_changes later — it only needs a
-- table name with the same shape (id / user_id / field / old_value / new_value /
-- detected_at).
--
-- Idempotent — safe to run repeatedly.

CREATE TABLE IF NOT EXISTS telegram_user_changes (
    id           BIGSERIAL    PRIMARY KEY,
    user_id      BIGINT       NOT NULL,
    field        VARCHAR(64)  NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    detected_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- "Recent changes for this user" — the dominant query pattern from the
-- user_intelligence dashboard.
CREATE INDEX IF NOT EXISTS idx_telegram_user_changes_user
    ON telegram_user_changes (user_id, detected_at DESC);

-- "Recent changes for this field" — used by ops when investigating a specific
-- mass-rename event (e.g. all usernames cycled in a 24h window).
CREATE INDEX IF NOT EXISTS idx_telegram_user_changes_field
    ON telegram_user_changes (field, detected_at DESC);

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_telegram_user_changes_field;
-- DROP INDEX IF EXISTS idx_telegram_user_changes_user;
-- DROP TABLE IF EXISTS telegram_user_changes;
