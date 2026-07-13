-- Migration: add youtube_user_changes table
-- Per-channel field diffs detected during YouTube ingestion.
-- Mirrors telegram_user_changes shape — backed by the same generic
-- src/core/user_change_tracker.UserChangeTracker module, whose INSERT writes
-- exactly (channel_id, field, old_value, new_value); detected_at is DB-defaulted.
--
-- The subject column is channel_id (not user_id) because YouTube's canonical
-- entity is youtube_channels.platform_channel_id (the "UC..." string id).
-- Read helpers must pass pk_col="channel_id".
--
-- Idempotent — safe to run repeatedly.

CREATE TABLE IF NOT EXISTS youtube_user_changes (
    id           BIGSERIAL    PRIMARY KEY,
    channel_id   VARCHAR(255) NOT NULL,
    field        VARCHAR(64)  NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    detected_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_youtube_user_changes_channel
    ON youtube_user_changes (channel_id, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_youtube_user_changes_field
    ON youtube_user_changes (field, detected_at DESC);

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_youtube_user_changes_field;
-- DROP INDEX IF EXISTS idx_youtube_user_changes_channel;
-- DROP TABLE IF EXISTS youtube_user_changes;
