-- Migration 002 — Schema-per-service architecture
-- Creates all new service schemas alongside existing public tables.
-- Idempotent: all statements use IF NOT EXISTS.
-- Run AFTER 001_initial_schema.sql.

-- =============================================================================
-- Extensions
-- =============================================================================
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =============================================================================
-- Schema: collector
-- The single source of truth for all ingested WhatsApp data.
-- Written exclusively by the collector service.
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS collector;

-- Registered WhatsApp sessions (one per Baileys instance / phone number)
CREATE TABLE IF NOT EXISTS collector.wa_sessions (
    id                  SERIAL PRIMARY KEY,
    session_name        VARCHAR(100) UNIQUE NOT NULL,
    phone_jid           VARCHAR(100),
    display_name        VARCHAR(255),
    status              VARCHAR(20) DEFAULT 'active',
    last_connected      TIMESTAMP,
    cooldown_until      TIMESTAMP,
    created_at          TIMESTAMP DEFAULT NOW()
);

-- Session lifecycle event audit trail
CREATE TABLE IF NOT EXISTS collector.session_events (
    id                  SERIAL PRIMARY KEY,
    session_name        VARCHAR(100) NOT NULL,
    event_type          VARCHAR(50) NOT NULL,
    detail              TEXT,
    occurred_at         TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_session_events_session ON collector.session_events(session_name);
CREATE INDEX IF NOT EXISTS idx_session_events_time    ON collector.session_events(occurred_at DESC);

-- All known chats
CREATE TABLE IF NOT EXISTS collector.chats (
    jid                 TEXT PRIMARY KEY,
    chat_type           VARCHAR(20) NOT NULL,
    name                TEXT,
    description         TEXT,
    photo_path          TEXT,
    member_count        INTEGER,
    is_community        BOOLEAN DEFAULT FALSE,
    community_jid       TEXT,
    created_at          TIMESTAMP,
    collected_at        TIMESTAMP DEFAULT NOW(),
    payload             JSONB
);

-- All known users / contacts (jid is PK — fixes BUG-03)
CREATE TABLE IF NOT EXISTS collector.users (
    jid                 TEXT PRIMARY KEY,
    lid                 TEXT,
    phone_number        VARCHAR(20),
    display_name        VARCHAR(255),
    push_name           VARCHAR(255),
    business_name       VARCHAR(255),
    is_business         BOOLEAN DEFAULT FALSE,
    is_verified         BOOLEAN DEFAULT FALSE,
    first_seen          TIMESTAMP DEFAULT NOW(),
    last_seen           TIMESTAMP DEFAULT NOW(),
    payload             JSONB
);
CREATE INDEX IF NOT EXISTS idx_users_lid ON collector.users(lid) WHERE lid IS NOT NULL;

-- JID ↔ LID cross-reference (standalone UNIQUE on lid removed — fixes BUG-03)
CREATE TABLE IF NOT EXISTS collector.jid_lid_map (
    jid                 TEXT NOT NULL,
    lid                 TEXT NOT NULL,
    session_name        VARCHAR(100) NOT NULL,
    mapped_at           TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (jid, session_name)
);

-- Every time a user is observed in a message or event (drives change detection)
CREATE TABLE IF NOT EXISTS collector.user_sightings (
    id                  BIGSERIAL PRIMARY KEY,
    user_jid            TEXT NOT NULL,
    seen_in_chat_jid    TEXT,
    seen_at             TIMESTAMP DEFAULT NOW(),
    payload             JSONB
);
CREATE INDEX IF NOT EXISTS idx_user_sightings_user ON collector.user_sightings(user_jid);
CREATE INDEX IF NOT EXISTS idx_user_sightings_time ON collector.user_sightings(seen_at DESC);

-- Historical profile photos per user
CREATE TABLE IF NOT EXISTS collector.user_profile_photos (
    id                  SERIAL PRIMARY KEY,
    user_jid            TEXT NOT NULL,
    photo_hash          VARCHAR(64),
    photo_path          TEXT,
    collected_at        TIMESTAMP DEFAULT NOW(),
    UNIQUE (user_jid, photo_hash)
);

-- Group participant membership snapshots
CREATE TABLE IF NOT EXISTS collector.group_participants (
    id                  SERIAL PRIMARY KEY,
    chat_jid            TEXT NOT NULL,
    user_jid            TEXT NOT NULL,
    role                VARCHAR(20),
    joined_at           TIMESTAMP,
    left_at             TIMESTAMP,
    seen_at             TIMESTAMP DEFAULT NOW(),
    UNIQUE (chat_jid, user_jid)
);
CREATE INDEX IF NOT EXISTS idx_group_participants_chat ON collector.group_participants(chat_jid);

-- The source of truth for all messages
-- UNIQUE on (message_id, chat_jid) — fixes BUG-01
CREATE TABLE IF NOT EXISTS collector.raw_messages (
    id                  BIGSERIAL PRIMARY KEY,
    message_id          TEXT NOT NULL,
    chat_jid            TEXT NOT NULL,
    chat_type           VARCHAR(20),
    sender_jid          TEXT,
    sender_lid          TEXT,
    session_name        VARCHAR(100) NOT NULL,
    message_type        VARCHAR(30),
    body                TEXT,
    has_media           BOOLEAN DEFAULT FALSE,
    is_forwarded        BOOLEAN DEFAULT FALSE,
    forwarding_score    INTEGER DEFAULT 0,
    quoted_msg_id       TEXT,
    is_edit             BOOLEAN DEFAULT FALSE,
    is_deleted          BOOLEAN DEFAULT FALSE,
    collected_at        TIMESTAMP NOT NULL DEFAULT NOW(),
    raw_payload         JSONB NOT NULL,
    UNIQUE (message_id, chat_jid)
);
CREATE INDEX IF NOT EXISTS idx_raw_messages_chat        ON collector.raw_messages(chat_jid);
CREATE INDEX IF NOT EXISTS idx_raw_messages_sender      ON collector.raw_messages(sender_jid);
CREATE INDEX IF NOT EXISTS idx_raw_messages_type        ON collector.raw_messages(message_type);
CREATE INDEX IF NOT EXISTS idx_raw_messages_has_media   ON collector.raw_messages(has_media) WHERE has_media = TRUE;
CREATE INDEX IF NOT EXISTS idx_raw_messages_collected   ON collector.raw_messages(collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_messages_session     ON collector.raw_messages(session_name);

-- Edit history per message
CREATE TABLE IF NOT EXISTS collector.message_edits (
    id                  SERIAL PRIMARY KEY,
    message_id          TEXT NOT NULL,
    chat_jid            TEXT NOT NULL,
    edited_at           TIMESTAMP DEFAULT NOW(),
    raw_payload         JSONB
);

-- Deletion events
CREATE TABLE IF NOT EXISTS collector.message_deletions (
    id                  SERIAL PRIMARY KEY,
    message_id          TEXT NOT NULL,
    chat_jid            TEXT NOT NULL,
    deleted_at          TIMESTAMP DEFAULT NOW()
);

-- Per-message reactions
CREATE TABLE IF NOT EXISTS collector.message_reactions (
    id                  SERIAL PRIMARY KEY,
    message_id          TEXT NOT NULL,
    chat_jid            TEXT NOT NULL,
    reactor_jid         TEXT,
    emoji               VARCHAR(50),
    reacted_at          TIMESTAMP DEFAULT NOW(),
    UNIQUE (message_id, chat_jid, reactor_jid)
);

-- Call telemetry
CREATE TABLE IF NOT EXISTS collector.calls (
    id                  SERIAL PRIMARY KEY,
    call_id             TEXT NOT NULL,
    from_jid            TEXT,
    chat_jid            TEXT,
    call_type           VARCHAR(20),
    status              VARCHAR(20),
    duration_seconds    INTEGER,
    session_name        VARCHAR(100),
    occurred_at         TIMESTAMP DEFAULT NOW(),
    raw_payload         JSONB
);

-- Durable backfill job queue (survives container restarts)
CREATE TABLE IF NOT EXISTS collector.backfill_jobs (
    id                  SERIAL PRIMARY KEY,
    session_name        VARCHAR(100) NOT NULL,
    chat_jid            TEXT NOT NULL,
    status              VARCHAR(20) DEFAULT 'pending',
    oldest_msg_key      JSONB,
    oldest_msg_ts       BIGINT,
    messages_done       INTEGER DEFAULT 0,
    cutoff_date         TIMESTAMP,
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW(),
    UNIQUE (session_name, chat_jid)
);

-- Per-service last consumed raw_messages.id (cursor-based fan-out)
CREATE TABLE IF NOT EXISTS collector.service_cursors (
    service_name        VARCHAR(50) PRIMARY KEY,
    last_message_id     BIGINT DEFAULT 0,
    updated_at          TIMESTAMP DEFAULT NOW()
);

-- Registered downstream services (used for safe pruning eligibility)
CREATE TABLE IF NOT EXISTS collector.service_registry (
    service_name        VARCHAR(50) PRIMARY KEY,
    is_active           BOOLEAN DEFAULT TRUE,
    registered_at       TIMESTAMP DEFAULT NOW()
);

-- =============================================================================
-- Schema: media_archival
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS media_archival;

CREATE TABLE IF NOT EXISTS media_archival.media_files (
    id                  SERIAL PRIMARY KEY,
    raw_message_id      BIGINT,
    message_id          TEXT NOT NULL,
    chat_jid            TEXT NOT NULL,
    file_unique_id      TEXT,
    mime_type           VARCHAR(100),
    file_size_bytes     BIGINT,
    by_id_path          TEXT,
    by_message_path     TEXT,
    sha256              VARCHAR(64),
    download_status     VARCHAR(20) DEFAULT 'pending',
    downloaded_at       TIMESTAMP,
    collected_at        TIMESTAMP DEFAULT NOW(),
    expiry_at           TIMESTAMP,
    UNIQUE (message_id, chat_jid)
);
CREATE INDEX IF NOT EXISTS idx_media_files_status  ON media_archival.media_files(download_status);
CREATE INDEX IF NOT EXISTS idx_media_files_expiry  ON media_archival.media_files(expiry_at) WHERE expiry_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_media_files_chat    ON media_archival.media_files(chat_jid);

CREATE TABLE IF NOT EXISTS media_archival.download_failures (
    id                  SERIAL PRIMARY KEY,
    message_id          TEXT NOT NULL,
    chat_jid            TEXT NOT NULL,
    error_message       TEXT,
    attempt_count       INTEGER DEFAULT 1,
    next_retry_at       TIMESTAMP,
    last_attempted_at   TIMESTAMP DEFAULT NOW(),
    is_permanent        BOOLEAN DEFAULT FALSE,
    UNIQUE (message_id, chat_jid)
);

-- =============================================================================
-- Schema: face_recognition
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS face_recognition;

CREATE TABLE IF NOT EXISTS face_recognition.identity_entities (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label               VARCHAR(255) DEFAULT 'Unknown',
    centroid            vector(128) NOT NULL,
    occurrence_count    INTEGER DEFAULT 1,
    first_seen          TIMESTAMP DEFAULT NOW(),
    last_seen           TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_identity_centroid ON face_recognition.identity_entities
    USING hnsw (centroid vector_l2_ops);

CREATE TABLE IF NOT EXISTS face_recognition.face_embeddings (
    id                  SERIAL PRIMARY KEY,
    identity_id         UUID REFERENCES face_recognition.identity_entities(id) ON DELETE SET NULL,
    embedding           vector(128) NOT NULL,
    source_message_id   TEXT NOT NULL,
    source_chat_jid     TEXT NOT NULL,
    frame_index         INTEGER DEFAULT 0,
    is_valid            BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_face_embeddings_identity ON face_recognition.face_embeddings(identity_id);
CREATE INDEX IF NOT EXISTS idx_face_embeddings_source   ON face_recognition.face_embeddings(source_message_id, source_chat_jid);

CREATE TABLE IF NOT EXISTS face_recognition.published_findings (
    id                  SERIAL PRIMARY KEY,
    identity_id         UUID REFERENCES face_recognition.identity_entities(id) ON DELETE CASCADE,
    source_message_id   TEXT NOT NULL,
    source_chat_jid     TEXT NOT NULL,
    findings_message_id TEXT,
    published_at        TIMESTAMP DEFAULT NOW(),
    UNIQUE (source_message_id, source_chat_jid, identity_id)
);

CREATE TABLE IF NOT EXISTS face_recognition.processed_media (
    id                  SERIAL PRIMARY KEY,
    source_message_id   TEXT NOT NULL,
    source_chat_jid     TEXT NOT NULL,
    faces_found         INTEGER DEFAULT 0,
    processed_at        TIMESTAMP DEFAULT NOW(),
    UNIQUE (source_message_id, source_chat_jid)
);

-- =============================================================================
-- Schema: user_intelligence
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS user_intelligence;

CREATE TABLE IF NOT EXISTS user_intelligence.user_history (
    id                  SERIAL PRIMARY KEY,
    user_jid            TEXT NOT NULL,
    field_name          VARCHAR(50) NOT NULL,
    old_value           TEXT,
    new_value           TEXT,
    changed_at          TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_user_history_user ON user_intelligence.user_history(user_jid);
CREATE INDEX IF NOT EXISTS idx_user_history_time ON user_intelligence.user_history(changed_at DESC);

CREATE TABLE IF NOT EXISTS user_intelligence.user_chat_memberships (
    user_jid            TEXT NOT NULL,
    chat_jid            TEXT NOT NULL,
    first_seen          TIMESTAMP DEFAULT NOW(),
    last_seen           TIMESTAMP DEFAULT NOW(),
    message_count       INTEGER DEFAULT 0,
    PRIMARY KEY (user_jid, chat_jid)
);
CREATE INDEX IF NOT EXISTS idx_memberships_chat ON user_intelligence.user_chat_memberships(chat_jid);

CREATE TABLE IF NOT EXISTS user_intelligence.user_connections (
    user_jid_a          TEXT NOT NULL,
    user_jid_b          TEXT NOT NULL,
    shared_chat_count   INTEGER DEFAULT 0,
    last_updated        TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (user_jid_a, user_jid_b),
    CHECK (user_jid_a < user_jid_b)
);

-- =============================================================================
-- Schema: link_discovery
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS link_discovery;

CREATE TABLE IF NOT EXISTS link_discovery.discovered_links (
    id                  SERIAL PRIMARY KEY,
    raw_message_id      BIGINT,
    link                TEXT NOT NULL,
    link_type           VARCHAR(30),
    status              VARCHAR(20) DEFAULT 'new',
    discovered_at       TIMESTAMP DEFAULT NOW(),
    UNIQUE (link)
);
CREATE INDEX IF NOT EXISTS idx_discovered_links_status ON link_discovery.discovered_links(status);
CREATE INDEX IF NOT EXISTS idx_discovered_links_type   ON link_discovery.discovered_links(link_type);

CREATE TABLE IF NOT EXISTS link_discovery.queue_rules (
    id                  SERIAL PRIMARY KEY,
    name                VARCHAR(100) NOT NULL,
    keyword_whitelist   TEXT[],
    keyword_blacklist   TEXT[],
    auto_queue          BOOLEAN DEFAULT FALSE,
    is_active           BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS link_discovery.join_queue (
    id                  SERIAL PRIMARY KEY,
    link                TEXT NOT NULL,
    session_name        VARCHAR(100),
    status              VARCHAR(20) DEFAULT 'pending',
    source              VARCHAR(50),
    added_at            TIMESTAMP DEFAULT NOW(),
    processed_at        TIMESTAMP,
    error               TEXT
);

-- =============================================================================
-- Schema: bulk_sender
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS bulk_sender;

CREATE TABLE IF NOT EXISTS bulk_sender.send_jobs (
    id                  SERIAL PRIMARY KEY,
    session_name        VARCHAR(100) NOT NULL,
    mode                VARCHAR(20) NOT NULL,
    source_type         VARCHAR(20) NOT NULL,
    source_path         TEXT,
    collector_query     JSONB,
    status              VARCHAR(20) DEFAULT 'pending',
    operator_confirmed  BOOLEAN DEFAULT FALSE,
    total_files         INTEGER DEFAULT 0,
    sent_count          INTEGER DEFAULT 0,
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bulk_sender.send_targets (
    id                  SERIAL PRIMARY KEY,
    job_id              INTEGER REFERENCES bulk_sender.send_jobs(id) ON DELETE CASCADE,
    chat_jid            TEXT NOT NULL,
    status              VARCHAR(20) DEFAULT 'pending',
    UNIQUE (job_id, chat_jid)
);

CREATE TABLE IF NOT EXISTS bulk_sender.sent_items (
    id                  SERIAL PRIMARY KEY,
    job_id              INTEGER REFERENCES bulk_sender.send_jobs(id) ON DELETE CASCADE,
    target_chat_jid     TEXT NOT NULL,
    file_path           TEXT NOT NULL,
    file_hash           VARCHAR(64) NOT NULL,
    sent_at             TIMESTAMP DEFAULT NOW(),
    wa_message_id       TEXT,
    UNIQUE (job_id, target_chat_jid, file_hash)
);

-- =============================================================================
-- Postgres users and permissions
-- Run as a superuser. Passwords should be set via env vars or secrets manager.
-- These are CREATE IF NOT EXISTS equivalents — DO blocks handle idempotency.
-- =============================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'collector_user') THEN
        CREATE USER collector_user WITH PASSWORD 'changeme_collector';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'media_archival_user') THEN
        CREATE USER media_archival_user WITH PASSWORD 'changeme_media';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'face_recog_user') THEN
        CREATE USER face_recog_user WITH PASSWORD 'changeme_face';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'user_intel_user') THEN
        CREATE USER user_intel_user WITH PASSWORD 'changeme_intel';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'link_disc_user') THEN
        CREATE USER link_disc_user WITH PASSWORD 'changeme_link';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'bulk_sender_user') THEN
        CREATE USER bulk_sender_user WITH PASSWORD 'changeme_bulk';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dashboard_user') THEN
        CREATE USER dashboard_user WITH PASSWORD 'changeme_dashboard';
    END IF;
END
$$;

-- collector_user: full access to collector schema
GRANT USAGE ON SCHEMA collector TO collector_user;
GRANT ALL ON ALL TABLES IN SCHEMA collector TO collector_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA collector TO collector_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA collector GRANT ALL ON TABLES TO collector_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA collector GRANT USAGE, SELECT ON SEQUENCES TO collector_user;

-- media_archival_user: own schema + read collector
GRANT USAGE ON SCHEMA media_archival TO media_archival_user;
GRANT ALL ON ALL TABLES IN SCHEMA media_archival TO media_archival_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA media_archival TO media_archival_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA media_archival GRANT ALL ON TABLES TO media_archival_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA media_archival GRANT USAGE, SELECT ON SEQUENCES TO media_archival_user;
GRANT USAGE ON SCHEMA collector TO media_archival_user;
GRANT SELECT ON ALL TABLES IN SCHEMA collector TO media_archival_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA collector GRANT SELECT ON TABLES TO media_archival_user;

-- face_recog_user: own schema + read collector + read media_archival
GRANT USAGE ON SCHEMA face_recognition TO face_recog_user;
GRANT ALL ON ALL TABLES IN SCHEMA face_recognition TO face_recog_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA face_recognition TO face_recog_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA face_recognition GRANT ALL ON TABLES TO face_recog_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA face_recognition GRANT USAGE, SELECT ON SEQUENCES TO face_recog_user;
GRANT USAGE ON SCHEMA collector TO face_recog_user;
GRANT SELECT ON ALL TABLES IN SCHEMA collector TO face_recog_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA collector GRANT SELECT ON TABLES TO face_recog_user;
GRANT USAGE ON SCHEMA media_archival TO face_recog_user;
GRANT SELECT ON ALL TABLES IN SCHEMA media_archival TO face_recog_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA media_archival GRANT SELECT ON TABLES TO face_recog_user;

-- user_intel_user: own schema + read collector
GRANT USAGE ON SCHEMA user_intelligence TO user_intel_user;
GRANT ALL ON ALL TABLES IN SCHEMA user_intelligence TO user_intel_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA user_intelligence TO user_intel_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA user_intelligence GRANT ALL ON TABLES TO user_intel_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA user_intelligence GRANT USAGE, SELECT ON SEQUENCES TO user_intel_user;
GRANT USAGE ON SCHEMA collector TO user_intel_user;
GRANT SELECT ON ALL TABLES IN SCHEMA collector TO user_intel_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA collector GRANT SELECT ON TABLES TO user_intel_user;

-- link_disc_user: own schema + read collector
GRANT USAGE ON SCHEMA link_discovery TO link_disc_user;
GRANT ALL ON ALL TABLES IN SCHEMA link_discovery TO link_disc_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA link_discovery TO link_disc_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA link_discovery GRANT ALL ON TABLES TO link_disc_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA link_discovery GRANT USAGE, SELECT ON SEQUENCES TO link_disc_user;
GRANT USAGE ON SCHEMA collector TO link_disc_user;
GRANT SELECT ON ALL TABLES IN SCHEMA collector TO link_disc_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA collector GRANT SELECT ON TABLES TO link_disc_user;

-- bulk_sender_user: own schema + read collector + read media_archival
GRANT USAGE ON SCHEMA bulk_sender TO bulk_sender_user;
GRANT ALL ON ALL TABLES IN SCHEMA bulk_sender TO bulk_sender_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA bulk_sender TO bulk_sender_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA bulk_sender GRANT ALL ON TABLES TO bulk_sender_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA bulk_sender GRANT USAGE, SELECT ON SEQUENCES TO bulk_sender_user;
GRANT USAGE ON SCHEMA collector TO bulk_sender_user;
GRANT SELECT ON ALL TABLES IN SCHEMA collector TO bulk_sender_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA collector GRANT SELECT ON TABLES TO bulk_sender_user;
GRANT USAGE ON SCHEMA media_archival TO bulk_sender_user;
GRANT SELECT ON ALL TABLES IN SCHEMA media_archival TO bulk_sender_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA media_archival GRANT SELECT ON TABLES TO bulk_sender_user;

-- dashboard_user: read-only across all schemas
GRANT USAGE ON SCHEMA collector TO dashboard_user;
GRANT USAGE ON SCHEMA media_archival TO dashboard_user;
GRANT USAGE ON SCHEMA face_recognition TO dashboard_user;
GRANT USAGE ON SCHEMA user_intelligence TO dashboard_user;
GRANT USAGE ON SCHEMA link_discovery TO dashboard_user;
GRANT USAGE ON SCHEMA bulk_sender TO dashboard_user;
GRANT SELECT ON ALL TABLES IN SCHEMA collector TO dashboard_user;
GRANT SELECT ON ALL TABLES IN SCHEMA media_archival TO dashboard_user;
GRANT SELECT ON ALL TABLES IN SCHEMA face_recognition TO dashboard_user;
GRANT SELECT ON ALL TABLES IN SCHEMA user_intelligence TO dashboard_user;
GRANT SELECT ON ALL TABLES IN SCHEMA link_discovery TO dashboard_user;
GRANT SELECT ON ALL TABLES IN SCHEMA bulk_sender TO dashboard_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA collector GRANT SELECT ON TABLES TO dashboard_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA media_archival GRANT SELECT ON TABLES TO dashboard_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA face_recognition GRANT SELECT ON TABLES TO dashboard_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA user_intelligence GRANT SELECT ON TABLES TO dashboard_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA link_discovery GRANT SELECT ON TABLES TO dashboard_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA bulk_sender GRANT SELECT ON TABLES TO dashboard_user;
