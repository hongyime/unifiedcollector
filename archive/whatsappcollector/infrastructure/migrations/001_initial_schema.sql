-- Migration 001 — Initial schema (existing public tables)
-- This is the original init-db.sql content, preserved as the first migration.
-- New services use the schema-per-service layout in 002_schema_per_service.sql.

-- Enable pgvector
CREATE EXTENSION IF NOT EXISTS vector;

-- Canonical user entity: maps JIDs and LIDs to a single record
CREATE TABLE IF NOT EXISTS users (
  user_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  jid           TEXT UNIQUE,
  lid           TEXT UNIQUE,
  display_name  TEXT,
  phone_number  TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Chat entity
CREATE TABLE IF NOT EXISTS chats (
  chat_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  jid           TEXT UNIQUE NOT NULL,
  chat_type     TEXT CHECK (chat_type IN ('dm','group','community','channel','status')),
  subject       TEXT,
  creator_jid   TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Messages
CREATE TABLE IF NOT EXISTS messages (
  message_id    TEXT PRIMARY KEY,
  chat_jid      TEXT NOT NULL,
  sender_jid    TEXT,
  sender_lid    TEXT,
  timestamp     TIMESTAMPTZ NOT NULL,
  message_type  TEXT,
  body          TEXT,
  is_forwarded  BOOLEAN DEFAULT FALSE,
  forward_score INT,
  quoted_msg_id TEXT,
  raw_payload   JSONB,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_jid_timestamp ON messages(chat_jid, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_sender_jid ON messages(sender_jid);

-- Media metadata
CREATE TABLE IF NOT EXISTS media (
  media_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id    TEXT REFERENCES messages(message_id),
  mime_type     TEXT NOT NULL,
  file_sha256   TEXT,
  file_size     BIGINT,
  local_path    TEXT,
  cdn_url       TEXT,
  download_key  BYTEA,
  is_downloaded BOOLEAN DEFAULT FALSE,
  expires_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_media_message_id ON media(message_id);
CREATE INDEX IF NOT EXISTS idx_media_file_sha256 ON media(file_sha256);

-- JID/LID cross-reference
CREATE TABLE IF NOT EXISTS jid_lid_map (
  jid         TEXT NOT NULL,
  lid         TEXT NOT NULL,
  resolved_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (jid, lid)
);

-- Profile Photos
CREATE TABLE IF NOT EXISTS profile_photos (
  photo_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_jid      TEXT NOT NULL,
  file_sha256   TEXT,
  local_path    TEXT,
  is_current    BOOLEAN DEFAULT TRUE,
  fetched_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Face embeddings (pgvector)
CREATE TABLE IF NOT EXISTS face_embeddings (
  embedding_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  media_id         UUID REFERENCES media(media_id),
  profile_photo_id UUID REFERENCES profile_photos(photo_id),
  sender_jid       TEXT,
  sender_lid       TEXT,
  identity_id      UUID,
  embedding        vector(128) NOT NULL,
  confidence       FLOAT,
  frame_offset     FLOAT,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_face_embeddings_hnsw ON face_embeddings USING hnsw (embedding vector_l2_ops) WITH (m = 24, ef_construction = 128);
CREATE INDEX IF NOT EXISTS idx_face_embeddings_identity_id ON face_embeddings(identity_id);

-- Identity clusters
CREATE TABLE IF NOT EXISTS identity_entities (
  identity_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  centroid         vector(128),
  occurrence_count INT DEFAULT 1,
  first_seen       TIMESTAMPTZ DEFAULT NOW(),
  last_seen        TIMESTAMPTZ DEFAULT NOW()
);

-- Multi-Account Sessions
CREATE TABLE IF NOT EXISTS wa_sessions (
  session_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_name   TEXT UNIQUE NOT NULL,
  phone_number   TEXT,
  status         TEXT DEFAULT 'disconnected',
  auth_dir_path  TEXT,
  cooldown_until TIMESTAMPTZ,
  created_at     TIMESTAMPTZ DEFAULT NOW(),
  updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Session Events (Health)
CREATE TABLE IF NOT EXISTS session_events (
  event_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id    UUID REFERENCES wa_sessions(session_id),
  event_type    TEXT NOT NULL,
  details       JSONB,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Group Participants
CREATE TABLE IF NOT EXISTS group_participants (
  group_jid TEXT NOT NULL,
  user_jid  TEXT NOT NULL,
  role      TEXT DEFAULT 'member',
  added_at  TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (group_jid, user_jid)
);

CREATE INDEX IF NOT EXISTS idx_group_participants_group ON group_participants(group_jid);

-- Call Log
CREATE TABLE IF NOT EXISTS calls (
  call_id      TEXT PRIMARY KEY,
  from_jid     TEXT NOT NULL,
  call_date    TIMESTAMPTZ NOT NULL,
  status       TEXT,
  is_video     BOOLEAN DEFAULT FALSE,
  is_group     BOOLEAN DEFAULT FALSE,
  session_name TEXT,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_calls_from_jid ON calls(from_jid);
CREATE INDEX IF NOT EXISTS idx_calls_date ON calls(call_date);

-- Performance Indexes
CREATE INDEX IF NOT EXISTS idx_users_phone_number ON users(phone_number);
CREATE INDEX IF NOT EXISTS idx_messages_sender_lid ON messages(sender_lid);
CREATE INDEX IF NOT EXISTS idx_chats_chat_type ON chats(chat_type);
CREATE INDEX IF NOT EXISTS idx_identity_entities_last_seen ON identity_entities(last_seen);
CREATE INDEX IF NOT EXISTS idx_profile_photos_user_jid ON profile_photos(user_jid);
CREATE INDEX IF NOT EXISTS idx_face_embeddings_sender_jid ON face_embeddings(sender_jid);
CREATE INDEX IF NOT EXISTS idx_face_embeddings_sender_lid ON face_embeddings(sender_lid) WHERE sender_lid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_face_embeddings_identity_sender ON face_embeddings(identity_id, sender_jid, sender_lid);
CREATE INDEX IF NOT EXISTS idx_media_expiry ON media(is_downloaded, expires_at) WHERE is_downloaded = false AND expires_at IS NOT NULL;
