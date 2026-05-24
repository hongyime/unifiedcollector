-- Migration 004 — Add session control columns to link_discovery.queue_rules
-- Adds preferred_session and session_allowlist to support per-rule session
-- preferences and allowlists in the link discovery pipeline.
-- Idempotent: uses IF NOT EXISTS guards.
-- Run AFTER 002_schema_per_service.sql.

ALTER TABLE link_discovery.queue_rules ADD COLUMN IF NOT EXISTS preferred_session TEXT;
ALTER TABLE link_discovery.queue_rules ADD COLUMN IF NOT EXISTS session_allowlist TEXT[];
