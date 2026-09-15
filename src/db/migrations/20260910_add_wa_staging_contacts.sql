-- Sprint 5: WhatsApp contact staging table for CDC-style async merge
-- Option 5 from docs/plans/whatsapp-contact-architecture-alternatives.md
--
-- UNLOGGED: writes skip WAL → order of magnitude less fsync than ordinary tables.
-- Data loss on Postgres crash is acceptable because RabbitMQ still holds the
-- original events and will re-deliver after restart.
--
-- The background merger (wa_staging_merge.py) reads this table on each tick
-- and runs:
--   INSERT INTO whatsapp_users SELECT DISTINCT ON (platform_user_id) ...
--   FROM wa_staging_contacts ORDER BY collected_at DESC
--   ON CONFLICT (platform_user_id) DO UPDATE SET ...
-- then TRUNCATE wa_staging_contacts.
--
-- Idempotent: IF NOT EXISTS guards.

CREATE UNLOGGED TABLE IF NOT EXISTS wa_staging_contacts (
    id                  BIGSERIAL PRIMARY KEY,
    platform_user_id    TEXT        NOT NULL,
    name                TEXT,
    pushname            TEXT,
    phone_number        TEXT,
    is_business         BOOLEAN,
    lid                 TEXT,           -- @lid value when present (for lid_map merge)
    phone_jid           TEXT,           -- resolved phone JID for lid_map rows
    collected_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index on collected_at for efficient keyset-based batch reads by the merger
CREATE INDEX IF NOT EXISTS wa_staging_contacts_collected_at_idx
    ON wa_staging_contacts (collected_at DESC);

-- Index on platform_user_id for DISTINCT ON in the merge query
CREATE INDEX IF NOT EXISTS wa_staging_contacts_platform_user_id_idx
    ON wa_staging_contacts (platform_user_id, collected_at DESC);
