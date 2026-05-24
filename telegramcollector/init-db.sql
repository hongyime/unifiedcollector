-- init-db.sql
-- telegramcollector database initialisation script
-- Runs once when the Postgres container is first created.
-- Safe to re-run on an existing database (all statements are idempotent).
--
-- Schemas:  collector, face_recognition, user_intelligence,
--           link_discovery, bulk_sender
-- Users:    collector_user, face_recog_user, user_intel_user,
--           link_disc_user, bulk_sender_user, dashboard_user

-- =============================================================================
-- Extensions
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =============================================================================
-- Schemas
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS collector;
CREATE SCHEMA IF NOT EXISTS face_recognition;
CREATE SCHEMA IF NOT EXISTS user_intelligence;
CREATE SCHEMA IF NOT EXISTS link_discovery;
CREATE SCHEMA IF NOT EXISTS bulk_sender;

-- =============================================================================
-- collector schema tables
-- =============================================================================

CREATE TABLE IF NOT EXISTS collector.telegram_accounts (
    id                SERIAL PRIMARY KEY,
    phone_number      VARCHAR(20) UNIQUE NOT NULL,
    display_name      VARCHAR(100),
    status            VARCHAR(20) DEFAULT 'active',
    is_admin_capable  BOOLEAN DEFAULT FALSE,
    session_file_path TEXT,
    last_error        TEXT,
    created_at        TIMESTAMP DEFAULT NOW(),
    last_active       TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS collector.chats (
    id             BIGINT PRIMARY KEY,
    type           VARCHAR(20),
    title          VARCHAR(255),
    username       VARCHAR(100),
    description    TEXT,
    photo_path     TEXT,
    member_count   INTEGER,
    is_admin       BOOLEAN DEFAULT FALSE,
    linked_chat_id BIGINT,
    created_at     TIMESTAMP,
    collected_at   TIMESTAMP DEFAULT NOW(),
    payload        JSONB
);

CREATE TABLE IF NOT EXISTS collector.chat_photo_history (
    id         SERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    photo_path TEXT,
    changed_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS collector.users (
    id          BIGINT PRIMARY KEY,
    username    VARCHAR(100),
    first_name  VARCHAR(255),
    last_name   VARCHAR(255),
    phone       VARCHAR(20),
    bio         TEXT,
    is_bot      BOOLEAN DEFAULT FALSE,
    is_verified BOOLEAN DEFAULT FALSE,
    is_premium  BOOLEAN DEFAULT FALSE,
    is_scam     BOOLEAN DEFAULT FALSE,
    is_fake     BOOLEAN DEFAULT FALSE,
    first_seen  TIMESTAMP DEFAULT NOW(),
    last_seen   TIMESTAMP DEFAULT NOW(),
    payload     JSONB
);

CREATE TABLE IF NOT EXISTS collector.user_sightings (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    seen_in_chat_id BIGINT,
    seen_at         TIMESTAMP DEFAULT NOW(),
    payload         JSONB
);

CREATE TABLE IF NOT EXISTS collector.user_profile_photos (
    id             SERIAL PRIMARY KEY,
    user_id        BIGINT NOT NULL,
    photo_id       BIGINT,
    file_unique_id VARCHAR(255),
    photo_path     TEXT,
    collected_at   TIMESTAMP DEFAULT NOW()
);

-- updated_at is required by AdminLogPoller._update_chat_member() upsert
CREATE TABLE IF NOT EXISTS collector.chat_members (
    id         SERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    role       VARCHAR(20),
    joined_at  TIMESTAMP,
    left_at    TIMESTAMP,
    seen_at    TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS collector.raw_messages (
    id                      BIGSERIAL PRIMARY KEY,
    chat_id                 BIGINT NOT NULL,
    message_id              BIGINT NOT NULL,
    sender_id               BIGINT,
    message_type            VARCHAR(30),
    has_media               BOOLEAN DEFAULT FALSE,
    media_path              TEXT,
    file_unique_id          VARCHAR(255),
    file_id                 VARCHAR(255),
    is_edit                 BOOLEAN DEFAULT FALSE,
    is_deleted              BOOLEAN DEFAULT FALSE,
    forward_from_chat_id    BIGINT,
    forward_from_message_id BIGINT,
    reply_to_message_id     BIGINT,
    views                   INTEGER,
    collected_at            TIMESTAMP NOT NULL DEFAULT NOW(),
    payload                 JSONB NOT NULL,
    UNIQUE(chat_id, message_id)
);

-- UNIQUE(chat_id, message_id) required for ON CONFLICT DO NOTHING in AdminLogPoller
CREATE TABLE IF NOT EXISTS collector.message_edits (
    id         SERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    edited_at  TIMESTAMP DEFAULT NOW(),
    payload    JSONB,
    UNIQUE(chat_id, message_id)
);

-- UNIQUE(chat_id, message_id) required for ON CONFLICT DO NOTHING in AdminLogPoller
CREATE TABLE IF NOT EXISTS collector.message_deletions (
    id         SERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    deleted_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS collector.message_reactions (
    id         SERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    user_id    BIGINT,
    reaction   VARCHAR(50),
    reacted_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(chat_id, message_id, user_id, reaction)
);

CREATE TABLE IF NOT EXISTS collector.polls (
    id           SERIAL PRIMARY KEY,
    chat_id      BIGINT NOT NULL,
    message_id   BIGINT NOT NULL,
    question     TEXT,
    options      JSONB,
    is_anonymous BOOLEAN DEFAULT TRUE,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS collector.poll_votes (
    id           SERIAL PRIMARY KEY,
    poll_id      INTEGER REFERENCES collector.polls(id) ON DELETE CASCADE,
    user_id      BIGINT NOT NULL,
    option_index INTEGER NOT NULL,
    voted_at     TIMESTAMP DEFAULT NOW(),
    UNIQUE(poll_id, user_id)
);

-- file_unique_id, file_id, collected_at included inline (no ALTER TABLE needed)
CREATE TABLE IF NOT EXISTS collector.stories (
    id             SERIAL PRIMARY KEY,
    story_id       BIGINT NOT NULL,
    peer_id        BIGINT NOT NULL,
    account_id     INTEGER REFERENCES collector.telegram_accounts(id),
    media_type     VARCHAR(20),
    media_path     TEXT,
    file_unique_id VARCHAR(255),
    file_id        VARCHAR(255),
    expire_date    TIMESTAMP,
    collected_at   TIMESTAMP DEFAULT NOW(),
    processed_at   TIMESTAMP DEFAULT NOW(),
    payload        JSONB,
    UNIQUE(story_id, peer_id, account_id)
);

CREATE TABLE IF NOT EXISTS collector.backfill_jobs (
    id              SERIAL PRIMARY KEY,
    account_id      INTEGER REFERENCES collector.telegram_accounts(id),
    chat_id         BIGINT NOT NULL,
    status          VARCHAR(20) DEFAULT 'pending',
    from_message_id BIGINT,
    to_message_id   BIGINT,
    messages_done   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(account_id, chat_id)
);

CREATE TABLE IF NOT EXISTS collector.backfill_state (
    id                        SERIAL PRIMARY KEY,
    chat_id                   BIGINT NOT NULL,
    account_id                INTEGER REFERENCES collector.telegram_accounts(id),
    status                    VARCHAR(20) DEFAULT 'pending',
    poll_type                 VARCHAR(20) DEFAULT 'backfill',
    last_processed_message_id BIGINT,
    last_event_id             BIGINT,
    started_at                TIMESTAMP,
    completed_at              TIMESTAMP,
    error                     TEXT,
    updated_at                TIMESTAMP DEFAULT NOW(),
    UNIQUE(chat_id, account_id, poll_type)
);

CREATE TABLE IF NOT EXISTS collector.group_join_queue (
    id              SERIAL PRIMARY KEY,
    link            TEXT NOT NULL,
    account_id      INTEGER REFERENCES collector.telegram_accounts(id),
    status          VARCHAR(20) DEFAULT 'pending',
    source          VARCHAR(50),
    language_filter BOOLEAN DEFAULT TRUE,
    added_at        TIMESTAMP DEFAULT NOW(),
    processed_at    TIMESTAMP,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS collector.scan_checkpoints (
    id                        SERIAL PRIMARY KEY,
    account_id                INTEGER REFERENCES collector.telegram_accounts(id),
    chat_id                   BIGINT NOT NULL,
    chat_type                 VARCHAR(20),
    last_processed_message_id BIGINT,
    last_seen_message_id      BIGINT DEFAULT 0,
    scan_mode                 VARCHAR(20) DEFAULT 'backfill',
    is_complete               BOOLEAN DEFAULT FALSE,
    last_updated              TIMESTAMP DEFAULT NOW(),
    UNIQUE(account_id, chat_id)
);

CREATE TABLE IF NOT EXISTS collector.service_cursors (
    service_name    VARCHAR(50) PRIMARY KEY,
    last_message_id BIGINT DEFAULT 0,
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS collector.admin_log_events (
    id           BIGSERIAL PRIMARY KEY,
    chat_id      BIGINT NOT NULL,
    event_id     BIGINT NOT NULL,
    event_type   VARCHAR(50) NOT NULL,
    user_id      BIGINT,
    message_id   BIGINT,
    event_data   JSONB,
    collected_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(chat_id, event_id)
);

CREATE TABLE IF NOT EXISTS collector.service_registry (
    service_name  VARCHAR(50) PRIMARY KEY,
    is_active     BOOLEAN DEFAULT TRUE,
    registered_at TIMESTAMP DEFAULT NOW()
);

-- Dashboard-managed configuration store + revisions + audit trail
CREATE TABLE IF NOT EXISTS collector.config_revisions (
    id         BIGSERIAL PRIMARY KEY,
    changed_by VARCHAR(120) DEFAULT 'dashboard',
    source     VARCHAR(50)  DEFAULT 'dashboard',
    notes      TEXT,
    created_at TIMESTAMP    DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS collector.config_settings (
    config_key      VARCHAR(120) PRIMARY KEY,
    group_name      VARCHAR(50) NOT NULL,
    value_plain     TEXT,
    value_encrypted BYTEA,
    is_sensitive    BOOLEAN NOT NULL DEFAULT FALSE,
    source          VARCHAR(50) DEFAULT 'dashboard',
    updated_by      VARCHAR(120) DEFAULT 'dashboard',
    revision_id     BIGINT REFERENCES collector.config_revisions(id) ON DELETE SET NULL,
    updated_at      TIMESTAMP DEFAULT NOW(),
    CHECK (
        (is_sensitive = FALSE AND value_plain IS NOT NULL)
        OR
        (is_sensitive = TRUE AND value_encrypted IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS collector.config_audit_log (
    id                BIGSERIAL PRIMARY KEY,
    config_key        VARCHAR(120) NOT NULL,
    group_name        VARCHAR(50) NOT NULL,
    old_value_masked  TEXT,
    new_value_masked  TEXT,
    old_value_hash    CHAR(64),
    new_value_hash    CHAR(64),
    changed_by        VARCHAR(120) DEFAULT 'dashboard',
    source            VARCHAR(50) DEFAULT 'dashboard',
    live_applied      BOOLEAN DEFAULT FALSE,
    restart_required  BOOLEAN DEFAULT TRUE,
    affected_services TEXT[] DEFAULT ARRAY[]::TEXT[],
    restart_status    VARCHAR(30),
    revision_id       BIGINT REFERENCES collector.config_revisions(id) ON DELETE SET NULL,
    created_at        TIMESTAMP DEFAULT NOW()
);

-- Channels polled by AdminLogPoller for admin log events
CREATE TABLE IF NOT EXISTS collector.monitored_chats (
    id         SERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    account_id INTEGER REFERENCES collector.telegram_accounts(id) ON DELETE CASCADE,
    is_active  BOOLEAN DEFAULT TRUE,
    added_at   TIMESTAMP DEFAULT NOW(),
    UNIQUE(chat_id, account_id)
);

-- Peers polled by StoryScanner for ephemeral story content
CREATE TABLE IF NOT EXISTS collector.monitored_peers (
    id         SERIAL PRIMARY KEY,
    peer_id    BIGINT NOT NULL,
    account_id INTEGER REFERENCES collector.telegram_accounts(id) ON DELETE CASCADE,
    is_active  BOOLEAN DEFAULT TRUE,
    added_at   TIMESTAMP DEFAULT NOW(),
    UNIQUE(peer_id, account_id)
);

-- =============================================================================
-- face_recognition schema tables
-- =============================================================================

CREATE TABLE IF NOT EXISTS face_recognition.telegram_topics (
    id                 SERIAL PRIMARY KEY,
    topic_id           BIGINT UNIQUE NOT NULL,
    label              VARCHAR(255) DEFAULT 'Unknown Person',
    face_count         INTEGER DEFAULT 0,
    message_count      INTEGER DEFAULT 0,
    exemplar_image_url TEXT,
    created_at         TIMESTAMP DEFAULT NOW(),
    updated_at         TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS face_recognition.face_embeddings (
    id                  SERIAL PRIMARY KEY,
    topic_id            INTEGER REFERENCES face_recognition.telegram_topics(id) ON DELETE CASCADE,
    embedding           vector(512) NOT NULL,
    source_chat_id      BIGINT NOT NULL,
    source_message_id   BIGINT NOT NULL,
    frame_index         INTEGER DEFAULT 0,
    quality_score       REAL DEFAULT 0.0,
    is_representative   BOOLEAN DEFAULT FALSE,
    detection_timestamp TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS face_recognition.uploaded_media (
    id                SERIAL PRIMARY KEY,
    source_chat_id    BIGINT NOT NULL,
    source_message_id BIGINT NOT NULL,
    topic_id          INTEGER REFERENCES face_recognition.telegram_topics(id),
    hub_message_id    BIGINT,
    created_at        TIMESTAMP DEFAULT NOW(),
    UNIQUE(source_chat_id, source_message_id, topic_id)
);

CREATE TABLE IF NOT EXISTS face_recognition.processed_media (
    id             SERIAL PRIMARY KEY,
    file_unique_id VARCHAR(255) UNIQUE NOT NULL,
    media_type     VARCHAR(20),
    faces_found    INTEGER DEFAULT 0,
    topics_matched TEXT[],
    processed_at   TIMESTAMP DEFAULT NOW()
);

-- =============================================================================
-- user_intelligence schema tables
-- =============================================================================

CREATE TABLE IF NOT EXISTS user_intelligence.user_history (
    id         SERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    field_name VARCHAR(50) NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    changed_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_intelligence.user_chat_memberships (
    user_id       BIGINT NOT NULL,
    chat_id       BIGINT NOT NULL,
    first_seen    TIMESTAMP,
    last_seen     TIMESTAMP,
    message_count INTEGER DEFAULT 0,
    PRIMARY KEY(user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS user_intelligence.user_connections (
    user_id_a         BIGINT NOT NULL,
    user_id_b         BIGINT NOT NULL,
    shared_chat_count INTEGER DEFAULT 0,
    last_updated      TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY(user_id_a, user_id_b),
    CHECK(user_id_a < user_id_b)
);

-- =============================================================================
-- link_discovery schema tables
-- =============================================================================

CREATE TABLE IF NOT EXISTS link_discovery.discovered_links (
    id             SERIAL PRIMARY KEY,
    raw_message_id BIGINT,
    link           TEXT NOT NULL,
    link_type      VARCHAR(30),
    chat_title     VARCHAR(255),
    language       VARCHAR(10),
    member_count   INTEGER,
    is_bot_link    BOOLEAN DEFAULT FALSE,
    status         VARCHAR(20) DEFAULT 'new',
    discovered_at  TIMESTAMP DEFAULT NOW(),
    UNIQUE(link)
);

CREATE TABLE IF NOT EXISTS link_discovery.queue_rules (
    id                 SERIAL PRIMARY KEY,
    name               VARCHAR(100) NOT NULL,
    language_whitelist TEXT[],
    language_blacklist TEXT[],
    keyword_whitelist  TEXT[],
    keyword_blacklist  TEXT[],
    min_member_count   INTEGER,
    max_member_count   INTEGER,
    auto_queue         BOOLEAN DEFAULT FALSE,
    is_active          BOOLEAN DEFAULT TRUE,
    created_at         TIMESTAMP DEFAULT NOW()
);

-- =============================================================================
-- bulk_sender schema tables
-- =============================================================================

CREATE TABLE IF NOT EXISTS bulk_sender.send_jobs (
    id              SERIAL PRIMARY KEY,
    account_id      INTEGER NOT NULL,
    target_chat_id  BIGINT NOT NULL,
    source_type     VARCHAR(20) NOT NULL,
    source_path     TEXT,
    collector_query JSONB,
    status          VARCHAR(20) DEFAULT 'pending',
    total_files     INTEGER DEFAULT 0,
    sent_count      INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bulk_sender.sent_items (
    id                  SERIAL PRIMARY KEY,
    job_id              INTEGER REFERENCES bulk_sender.send_jobs(id) ON DELETE CASCADE,
    file_path           TEXT NOT NULL,
    file_hash           VARCHAR(64) NOT NULL,
    sent_at             TIMESTAMP DEFAULT NOW(),
    telegram_message_id BIGINT,
    UNIQUE(job_id, file_hash)
);

-- =============================================================================
-- Indexes
-- =============================================================================

-- user_sightings
CREATE INDEX IF NOT EXISTS idx_user_sightings_user ON collector.user_sightings(user_id);
CREATE INDEX IF NOT EXISTS idx_user_sightings_time ON collector.user_sightings(seen_at DESC);

-- raw_messages
CREATE INDEX IF NOT EXISTS idx_raw_messages_chat        ON collector.raw_messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_raw_messages_type        ON collector.raw_messages(message_type);
CREATE INDEX IF NOT EXISTS idx_raw_messages_media       ON collector.raw_messages(has_media) WHERE has_media = TRUE;
CREATE INDEX IF NOT EXISTS idx_raw_messages_collected   ON collector.raw_messages(collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_messages_sender      ON collector.raw_messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_raw_messages_file_unique ON collector.raw_messages(file_unique_id);

-- face_embeddings — IVFFlat index for cosine similarity search
CREATE INDEX IF NOT EXISTS idx_face_embeddings_vector ON face_recognition.face_embeddings
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1000);
CREATE INDEX IF NOT EXISTS idx_face_embeddings_topic   ON face_recognition.face_embeddings(topic_id);
CREATE INDEX IF NOT EXISTS idx_face_embeddings_quality ON face_recognition.face_embeddings(quality_score DESC);

-- user_history
CREATE INDEX IF NOT EXISTS idx_user_history_user ON user_intelligence.user_history(user_id);
CREATE INDEX IF NOT EXISTS idx_user_history_time ON user_intelligence.user_history(changed_at DESC);

-- discovered_links
CREATE INDEX IF NOT EXISTS idx_discovered_links_status ON link_discovery.discovered_links(status);
CREATE INDEX IF NOT EXISTS idx_discovered_links_type   ON link_discovery.discovered_links(link_type);

-- admin_log_events
CREATE INDEX IF NOT EXISTS idx_admin_log_events_chat ON collector.admin_log_events(chat_id);
CREATE INDEX IF NOT EXISTS idx_admin_log_events_type ON collector.admin_log_events(event_type);
CREATE INDEX IF NOT EXISTS idx_admin_log_events_time ON collector.admin_log_events(collected_at DESC);

-- stories
CREATE INDEX IF NOT EXISTS idx_stories_peer    ON collector.stories(peer_id);
CREATE INDEX IF NOT EXISTS idx_stories_account ON collector.stories(account_id);
CREATE INDEX IF NOT EXISTS idx_stories_expire  ON collector.stories(expire_date);

-- monitored_chats / monitored_peers
CREATE INDEX IF NOT EXISTS idx_monitored_chats_account ON collector.monitored_chats(account_id);
CREATE INDEX IF NOT EXISTS idx_monitored_peers_account ON collector.monitored_peers(account_id);

-- config state / revision / audit
CREATE INDEX IF NOT EXISTS idx_config_revisions_created ON collector.config_revisions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_config_settings_group ON collector.config_settings(group_name);
CREATE INDEX IF NOT EXISTS idx_config_settings_updated ON collector.config_settings(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_config_audit_key_time ON collector.config_audit_log(config_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_config_audit_revision ON collector.config_audit_log(revision_id);

-- =============================================================================
-- Database users
-- =============================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'collector_user') THEN
        CREATE USER collector_user WITH PASSWORD 'collector_password';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'face_recog_user') THEN
        CREATE USER face_recog_user WITH PASSWORD 'face_recog_password';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'user_intel_user') THEN
        CREATE USER user_intel_user WITH PASSWORD 'user_intel_password';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'link_disc_user') THEN
        CREATE USER link_disc_user WITH PASSWORD 'link_disc_password';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'bulk_sender_user') THEN
        CREATE USER bulk_sender_user WITH PASSWORD 'bulk_sender_password';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'dashboard_user') THEN
        CREATE USER dashboard_user WITH PASSWORD 'dashboard_password';
    END IF;
END
$$;

-- =============================================================================
-- Permission grants
-- =============================================================================

-- collector_user: full access to collector schema
GRANT USAGE ON SCHEMA collector TO collector_user;
GRANT ALL ON ALL TABLES    IN SCHEMA collector TO collector_user;
GRANT ALL ON ALL SEQUENCES IN SCHEMA collector TO collector_user;

-- face_recog_user: full access to face_recognition + SELECT on collector
GRANT USAGE ON SCHEMA face_recognition TO face_recog_user;
GRANT ALL ON ALL TABLES    IN SCHEMA face_recognition TO face_recog_user;
GRANT ALL ON ALL SEQUENCES IN SCHEMA face_recognition TO face_recog_user;
GRANT USAGE  ON SCHEMA collector TO face_recog_user;
GRANT SELECT ON ALL TABLES IN SCHEMA collector TO face_recog_user;

-- user_intel_user: full access to user_intelligence + SELECT on collector
GRANT USAGE ON SCHEMA user_intelligence TO user_intel_user;
GRANT ALL ON ALL TABLES    IN SCHEMA user_intelligence TO user_intel_user;
GRANT ALL ON ALL SEQUENCES IN SCHEMA user_intelligence TO user_intel_user;
GRANT USAGE  ON SCHEMA collector TO user_intel_user;
GRANT SELECT ON ALL TABLES IN SCHEMA collector TO user_intel_user;

-- link_disc_user: full access to link_discovery + SELECT on collector
--                 + INSERT on collector.group_join_queue
GRANT USAGE ON SCHEMA link_discovery TO link_disc_user;
GRANT ALL ON ALL TABLES    IN SCHEMA link_discovery TO link_disc_user;
GRANT ALL ON ALL SEQUENCES IN SCHEMA link_discovery TO link_disc_user;
GRANT USAGE  ON SCHEMA collector TO link_disc_user;
GRANT SELECT ON ALL TABLES IN SCHEMA collector TO link_disc_user;
GRANT INSERT ON collector.group_join_queue TO link_disc_user;

-- bulk_sender_user: full access to bulk_sender + SELECT on collector
GRANT USAGE ON SCHEMA bulk_sender TO bulk_sender_user;
GRANT ALL ON ALL TABLES    IN SCHEMA bulk_sender TO bulk_sender_user;
GRANT ALL ON ALL SEQUENCES IN SCHEMA bulk_sender TO bulk_sender_user;
GRANT USAGE  ON SCHEMA collector TO bulk_sender_user;
GRANT SELECT ON ALL TABLES IN SCHEMA collector TO bulk_sender_user;

-- dashboard_user: read-only across all schemas
GRANT USAGE  ON SCHEMA collector         TO dashboard_user;
GRANT USAGE  ON SCHEMA face_recognition  TO dashboard_user;
GRANT USAGE  ON SCHEMA user_intelligence TO dashboard_user;
GRANT USAGE  ON SCHEMA link_discovery    TO dashboard_user;
GRANT USAGE  ON SCHEMA bulk_sender       TO dashboard_user;
GRANT SELECT ON ALL TABLES IN SCHEMA collector         TO dashboard_user;
GRANT SELECT ON ALL TABLES IN SCHEMA face_recognition  TO dashboard_user;
GRANT SELECT ON ALL TABLES IN SCHEMA user_intelligence TO dashboard_user;
GRANT SELECT ON ALL TABLES IN SCHEMA link_discovery    TO dashboard_user;
GRANT SELECT ON ALL TABLES IN SCHEMA bulk_sender       TO dashboard_user;

-- Dashboard config-control write path
GRANT INSERT ON collector.config_revisions TO dashboard_user;
GRANT SELECT, INSERT, UPDATE ON collector.config_settings TO dashboard_user;
GRANT INSERT ON collector.config_audit_log TO dashboard_user;
GRANT USAGE, SELECT ON SEQUENCE collector.config_revisions_id_seq TO dashboard_user;
GRANT USAGE, SELECT ON SEQUENCE collector.config_audit_log_id_seq TO dashboard_user;

-- =============================================================================
-- Maintenance functions
-- =============================================================================

CREATE OR REPLACE FUNCTION update_topic_stats(p_topic_id INTEGER)
RETURNS void AS $$
BEGIN
    UPDATE face_recognition.telegram_topics
    SET
        face_count    = (SELECT COUNT(*) FROM face_recognition.face_embeddings
                         WHERE topic_id = p_topic_id),
        message_count = (SELECT COUNT(*) FROM face_recognition.uploaded_media
                         WHERE topic_id = p_topic_id),
        updated_at    = NOW()
    WHERE id = p_topic_id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION cleanup_old_metrics(retention_days INTEGER DEFAULT 30)
RETURNS void AS $$
BEGIN
    -- Forward-compatible stub. Add DELETE statements when metrics tables are introduced.
    RETURN;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- Legacy / compatibility stubs (referenced by existing tests)
-- =============================================================================

CREATE SEQUENCE IF NOT EXISTS topic_reservation_seq START 1;

CREATE TABLE IF NOT EXISTS processing_metrics (
    id           SERIAL PRIMARY KEY,
    metric_name  VARCHAR(100) NOT NULL,
    metric_value REAL NOT NULL,
    recorded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_metrics_name ON processing_metrics(metric_name);
CREATE INDEX IF NOT EXISTS idx_metrics_time ON processing_metrics(recorded_at DESC);

-- collector.processing_errors — operational error log used by shared/database.py
CREATE TABLE IF NOT EXISTS collector.processing_errors (
    id            BIGSERIAL PRIMARY KEY,
    error_type    VARCHAR(100) NOT NULL,
    error_message TEXT,
    error_context JSONB,
    occurred_at   TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_processing_errors_type ON collector.processing_errors(error_type);
CREATE INDEX IF NOT EXISTS idx_processing_errors_time ON collector.processing_errors(occurred_at DESC);

-- Migration: add chat_type to existing deployments (idempotent)
ALTER TABLE collector.scan_checkpoints ADD COLUMN IF NOT EXISTS chat_type VARCHAR(20);

-- Public-schema compatibility stubs (allow unqualified table references in legacy code)
CREATE TABLE IF NOT EXISTS face_embeddings    (id SERIAL PRIMARY KEY);
CREATE TABLE IF NOT EXISTS health_checks      (id SERIAL PRIMARY KEY);
CREATE TABLE IF NOT EXISTS processing_errors  (id SERIAL PRIMARY KEY);
CREATE TABLE IF NOT EXISTS scan_checkpoints   (id SERIAL PRIMARY KEY);
CREATE TABLE IF NOT EXISTS telegram_accounts  (id SERIAL PRIMARY KEY);
CREATE TABLE IF NOT EXISTS telegram_topics    (id SERIAL PRIMARY KEY);

-- =============================================================================
-- Seed data
-- =============================================================================

INSERT INTO collector.service_registry (service_name, is_active) VALUES
    ('face_recognition',  TRUE),
    ('user_intelligence', TRUE),
    ('link_discovery',    TRUE),
    ('bulk_sender',       TRUE)
ON CONFLICT DO NOTHING;

INSERT INTO collector.service_cursors (service_name, last_message_id) VALUES
    ('face_recognition',  0),
    ('user_intelligence', 0),
    ('link_discovery',    0),
    ('bulk_sender',       0)
ON CONFLICT DO NOTHING;
