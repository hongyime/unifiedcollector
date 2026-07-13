-- Migration: add whatsapp_user_changes table
-- Per-user display-name diffs (name / pushname / is_business) detected during
-- WhatsApp ingestion.
-- Mirrors telegram_user_changes shape — backed by the same generic
-- src/core/user_change_tracker.UserChangeTracker module, whose INSERT writes
-- exactly (user_id, field, old_value, new_value); detected_at is DB-defaulted.
--
-- user_id is VARCHAR(255): it holds whatsapp_users.platform_user_id (the JID,
-- e.g. 15551234567@s.whatsapp.net, or an @lid fallback).
--
-- Idempotent — safe to run repeatedly.

CREATE TABLE IF NOT EXISTS whatsapp_user_changes (
    id           BIGSERIAL    PRIMARY KEY,
    user_id      VARCHAR(255) NOT NULL,
    field        VARCHAR(64)  NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    detected_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_user_changes_user
    ON whatsapp_user_changes (user_id, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_whatsapp_user_changes_field
    ON whatsapp_user_changes (field, detected_at DESC);

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_whatsapp_user_changes_field;
-- DROP INDEX IF EXISTS idx_whatsapp_user_changes_user;
-- DROP TABLE IF EXISTS whatsapp_user_changes;
