-- WhatsApp extended schema: face recognition, user intelligence, link discovery
-- Requires pgvector extension for embedding storage

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS wa_face_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label VARCHAR(255) NOT NULL DEFAULT 'Unknown',
    centroid vector(128),
    occurrence_count INT NOT NULL DEFAULT 1,
    confidence_avg FLOAT DEFAULT 0.0,
    first_seen TIMESTAMP NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMP NOT NULL DEFAULT NOW(),
    metadata JSONB
);

CREATE TABLE IF NOT EXISTS wa_face_embeddings (
    id SERIAL PRIMARY KEY,
    identity_id UUID REFERENCES wa_face_identities(id) ON DELETE CASCADE,
    embedding vector(128) NOT NULL,
    source_content_id VARCHAR(100),
    source_entity_id VARCHAR(100),
    frame_index INT DEFAULT 0,
    confidence FLOAT DEFAULT 0.0,
    bbox_x INT,
    bbox_y INT,
    bbox_w INT,
    bbox_h INT,
    is_valid BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wfe_identity ON wa_face_embeddings(identity_id);
CREATE INDEX IF NOT EXISTS idx_wfe_source ON wa_face_embeddings(source_content_id);

CREATE TABLE IF NOT EXISTS wa_user_profiles (
    jid TEXT PRIMARY KEY,
    display_name VARCHAR(255),
    push_name VARCHAR(255),
    phone_number VARCHAR(30),
    is_business BOOLEAN NOT NULL DEFAULT FALSE,
    status_text TEXT,
    profile_pic_url TEXT,
    first_seen TIMESTAMP NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMP NOT NULL DEFAULT NOW(),
    message_count INT NOT NULL DEFAULT 0,
    payload JSONB
);

CREATE INDEX IF NOT EXISTS idx_wup_name ON wa_user_profiles(push_name);
CREATE INDEX IF NOT EXISTS idx_wup_phone ON wa_user_profiles(phone_number);

CREATE TABLE IF NOT EXISTS wa_user_history (
    id SERIAL PRIMARY KEY,
    user_jid TEXT NOT NULL,
    field_name VARCHAR(50) NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wuh_jid ON wa_user_history(user_jid, changed_at DESC);

CREATE TABLE IF NOT EXISTS wa_discovered_links (
    id SERIAL PRIMARY KEY,
    link TEXT NOT NULL UNIQUE,
    link_type VARCHAR(30) NOT NULL,
    source_chat_jid TEXT,
    source_content_id VARCHAR(100),
    source_message_text TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'new',
    discovered_at TIMESTAMP NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_wdl_status ON wa_discovered_links(status);
CREATE INDEX IF NOT EXISTS idx_wdl_type ON wa_discovered_links(link_type);
CREATE INDEX IF NOT EXISTS idx_wdl_chat ON wa_discovered_links(source_chat_jid);
