-- Migration: add github_user_changes table
-- Per-user profile-field diffs detected during GitHub ingestion.
-- Mirrors telegram_user_changes shape — backed by the same generic
-- src/core/user_change_tracker.UserChangeTracker module, whose INSERT writes
-- exactly (user_id, field, old_value, new_value); detected_at is DB-defaulted.
--
-- user_id is BIGINT to match GitHub's numeric account id
-- (github_users.platform_user_id).
--
-- Idempotent — safe to run repeatedly.

CREATE TABLE IF NOT EXISTS github_user_changes (
    id           BIGSERIAL    PRIMARY KEY,
    user_id      BIGINT       NOT NULL,
    field        VARCHAR(64)  NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    detected_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_github_user_changes_user
    ON github_user_changes (user_id, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_github_user_changes_field
    ON github_user_changes (field, detected_at DESC);

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_github_user_changes_field;
-- DROP INDEX IF EXISTS idx_github_user_changes_user;
-- DROP TABLE IF EXISTS github_user_changes;
