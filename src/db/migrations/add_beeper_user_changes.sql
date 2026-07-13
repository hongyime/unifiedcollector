-- Migration: add beeper_user_changes table
-- Per-participant profile-field diffs (username / full_name / img_url)
-- detected during Beeper shadow-participant ingestion.
-- Mirrors telegram_user_changes shape — backed by the same generic
-- src/core/user_change_tracker.UserChangeTracker module, whose INSERT writes
-- exactly (user_id, field, old_value, new_value); detected_at is DB-defaulted.
--
-- user_id is TEXT: it holds beeper_shadow_participants.participant_id, which
-- can be long network-scoped ids (e.g. Matrix MXIDs) with no safe 255 cap.
-- Diffs are computed against the per-(chat, participant) row but logged under
-- the participant_id alone.
--
-- Idempotent — safe to run repeatedly.

CREATE TABLE IF NOT EXISTS beeper_user_changes (
    id           BIGSERIAL    PRIMARY KEY,
    user_id      TEXT         NOT NULL,
    field        VARCHAR(64)  NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    detected_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_beeper_user_changes_user
    ON beeper_user_changes (user_id, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_beeper_user_changes_field
    ON beeper_user_changes (field, detected_at DESC);

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_beeper_user_changes_field;
-- DROP INDEX IF EXISTS idx_beeper_user_changes_user;
-- DROP TABLE IF EXISTS beeper_user_changes;
