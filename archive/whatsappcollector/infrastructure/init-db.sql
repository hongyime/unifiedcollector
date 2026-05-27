-- init-db.sql — Bootstrap all schemas for a fresh Postgres instance.
-- Idempotent: all statements use IF NOT EXISTS.
-- This file is used for local dev / Docker first-run.
-- In production, use infrastructure/migrations/ via run_migrations.py.

-- =============================================================================
-- Extensions
-- =============================================================================
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =============================================================================
-- Schema: collector
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS collector;

CREATE TABLE IF NOT EXISTS collector.wa_sessions (
    id                  SERIAL PRIMARY KEY,
    session_name        VARCHAR(100) UNIQUE NOT NULL,
    phone_jid           VARCHAR(100),
    display_name        VARCHAR(255),
    status              VARCHAR(20) DEFAULT 'disconnected',
    last_connected      TIMESTAMP,
    cooldown_until      TIMESTAMP,
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS collector.session_events (
    id                  SERIAL PRIMARY KEY,
    session_name        VARCHAR(100) NOT NULL,
    event_type          VARCHAR(50) NOT NULL,
    detail              TEXT,
    occurred_at         TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_session_events_session ON collector.session_events(session_name);
CREATE INDEX IF NOT EXISTS idx_session_events_time    ON collector.session_events(occurred_at DESC);

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
CREATE INDEX IF NOT EXISTS idx_users_phone_number ON collector.users(phone_number) WHERE phone_number IS NOT NULL;

CREATE TABLE IF NOT EXISTS collector.jid_lid_map (
    jid                 TEXT NOT NULL,
    lid                 TEXT NOT NULL,
    session_name        VARCHAR(100) NOT NULL,
    mapped_at           TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (jid, session_name)
);

CREATE TABLE IF NOT EXISTS collector.user_sightings (
    id                  BIGSERIAL PRIMARY KEY,
    user_jid            TEXT NOT NULL,
    seen_in_chat_jid    TEXT,
    source_message_id   TEXT,
    source_chat_jid     TEXT,
    session_name        VARCHAR(100),
    seen_at             TIMESTAMP DEFAULT NOW(),
    payload             JSONB,
    UNIQUE (user_jid, seen_in_chat_jid, source_message_id, source_chat_jid)
);
CREATE INDEX IF NOT EXISTS idx_user_sightings_user ON collector.user_sightings(user_jid);
CREATE INDEX IF NOT EXISTS idx_user_sightings_time ON collector.user_sightings(seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_sightings_source_message ON collector.user_sightings(source_message_id);

CREATE TABLE IF NOT EXISTS collector.user_profile_photos (
    id                  SERIAL PRIMARY KEY,
    user_jid            TEXT NOT NULL,
    photo_hash          VARCHAR(64),
    photo_path          TEXT,
    collected_at        TIMESTAMP DEFAULT NOW(),
    UNIQUE (user_jid, photo_hash)
);

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

CREATE TABLE IF NOT EXISTS collector.message_edits (
    id                  SERIAL PRIMARY KEY,
    message_id          TEXT NOT NULL,
    chat_jid            TEXT NOT NULL,
    edited_at           TIMESTAMP DEFAULT NOW(),
    raw_payload         JSONB
);

CREATE TABLE IF NOT EXISTS collector.message_deletions (
    id                  SERIAL PRIMARY KEY,
    message_id          TEXT NOT NULL,
    chat_jid            TEXT NOT NULL,
    deleted_at          TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS collector.message_reactions (
    id                  SERIAL PRIMARY KEY,
    message_id          TEXT NOT NULL,
    chat_jid            TEXT NOT NULL,
    reactor_jid         TEXT,
    emoji               VARCHAR(50),
    reacted_at          TIMESTAMP DEFAULT NOW(),
    UNIQUE (message_id, chat_jid, reactor_jid)
);

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
CREATE INDEX IF NOT EXISTS idx_backfill_jobs_status ON collector.backfill_jobs(status);
CREATE INDEX IF NOT EXISTS idx_backfill_jobs_session ON collector.backfill_jobs(session_name);

CREATE TABLE IF NOT EXISTS collector.service_cursors (
    service_name        VARCHAR(50) PRIMARY KEY,
    last_message_id     BIGINT DEFAULT 0,
    updated_at          TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS collector.service_registry (
    service_name        VARCHAR(50) PRIMARY KEY,
    is_active           BOOLEAN DEFAULT TRUE,
    registered_at       TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS collector.system_config (
    key                 VARCHAR(100) PRIMARY KEY,
    value               TEXT,
    updated_at          TIMESTAMP DEFAULT NOW()
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
-- m=32 doubles connections vs default 16 → better recall at query time.
-- ef_construction=200 builds a higher-quality graph vs default 64.
-- Set hnsw.ef_search=100 at query time (default 40) for better recall.
CREATE INDEX IF NOT EXISTS idx_identity_centroid ON face_recognition.identity_entities
    USING hnsw (centroid vector_l2_ops) WITH (m = 32, ef_construction = 200);

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
CREATE INDEX IF NOT EXISTS idx_join_queue_status ON link_discovery.join_queue(status);
CREATE INDEX IF NOT EXISTS idx_join_queue_session ON link_discovery.join_queue(session_name) WHERE session_name IS NOT NULL;

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
CREATE INDEX IF NOT EXISTS idx_send_jobs_status ON bulk_sender.send_jobs(status);
CREATE INDEX IF NOT EXISTS idx_send_jobs_session ON bulk_sender.send_jobs(session_name);

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
