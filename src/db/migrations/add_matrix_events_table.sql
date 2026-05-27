-- Migration: add matrix_events table
-- Wave 1 Phase 1: persist normalized Matrix events ingested by the
-- MatrixCollector. One row per event_id (server-assigned, globally unique
-- per Matrix spec → safe PRIMARY KEY).
--
-- Idempotent — safe to run repeatedly.
--
-- Lifecycle of a row:
--   1. Plaintext event arrives via /sync timeline → INSERT with body filled,
--      is_encrypted=FALSE.
--   2. Encrypted event arrives that nio could NOT decrypt → INSERT with
--      body=NULL, is_encrypted=TRUE, is_decrypted=FALSE,
--      raw_content={the encrypted blob}.  Decryption worker later picks it
--      up via the idx_matrix_events_undecrypted index.
--   3. Decryption worker recovers keys → UPDATE body, raw_content,
--      is_decrypted=TRUE.
--   4. If the event has attached media (mxc:// URI), the media-download
--      worker UPDATEs media_local_path + media_sha256 once the file is
--      stored under Z:.
--
-- Reactions, edits, and replies all live in this single table; the
-- relates_to/relation_type columns let us reconstruct threads later
-- without a join table.

CREATE TABLE IF NOT EXISTS matrix_events (
    event_id          VARCHAR(256) PRIMARY KEY,
    room_id           VARCHAR(256) NOT NULL,
    sender            VARCHAR(256) NOT NULL,
    event_type        VARCHAR(64)  NOT NULL,        -- m.room.message, m.room.encrypted, m.reaction, ...
    msgtype           VARCHAR(32),                  -- m.text/m.image/m.video/m.audio/m.file (NULL if not a message)
    body              TEXT,                          -- decrypted plaintext, NULL if encrypted+undecryptable
    raw_content       JSONB NOT NULL,                -- full event content (decrypted if possible)
    formatted_body    TEXT,                          -- HTML-formatted body if present
    is_encrypted      BOOLEAN NOT NULL DEFAULT FALSE,
    is_decrypted      BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE only if was encrypted AND we successfully decrypted
    media_mxc         VARCHAR(512),                  -- mxc:// URI if event has attached media
    media_local_path  TEXT,                          -- absolute path under Z: once downloaded (NULL until then)
    media_sha256      VARCHAR(64),                   -- when downloaded
    server_ts         TIMESTAMPTZ NOT NULL,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- relations
    relates_to        VARCHAR(256),                  -- m.relates_to.event_id (replies/edits/reactions)
    relation_type     VARCHAR(32),                   -- m.in_reply_to / m.replace / m.annotation
    -- denormalized hints for fast queries
    is_edit           BOOLEAN NOT NULL DEFAULT FALSE,
    is_reaction       BOOLEAN NOT NULL DEFAULT FALSE,
    is_redacted       BOOLEAN NOT NULL DEFAULT FALSE
);

-- Hot path: timeline reads per room, newest first.
CREATE INDEX IF NOT EXISTS idx_matrix_events_room_ts
    ON matrix_events (room_id, server_ts DESC);

-- Audits / per-sender history.
CREATE INDEX IF NOT EXISTS idx_matrix_events_sender
    ON matrix_events (sender);

-- Thread reconstruction (replies / edits / reactions point at parent).
CREATE INDEX IF NOT EXISTS idx_matrix_events_relates
    ON matrix_events (relates_to)
    WHERE relates_to IS NOT NULL;

-- Media download worker scans this.
CREATE INDEX IF NOT EXISTS idx_matrix_events_media
    ON matrix_events (media_mxc)
    WHERE media_mxc IS NOT NULL;

-- Decryption worker scans this — partial index keeps it tiny once we
-- catch up on backfill.
CREATE INDEX IF NOT EXISTS idx_matrix_events_undecrypted
    ON matrix_events (is_encrypted)
    WHERE is_encrypted = TRUE AND is_decrypted = FALSE;

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_matrix_events_undecrypted;
-- DROP INDEX IF EXISTS idx_matrix_events_media;
-- DROP INDEX IF EXISTS idx_matrix_events_relates;
-- DROP INDEX IF EXISTS idx_matrix_events_sender;
-- DROP INDEX IF EXISTS idx_matrix_events_room_ts;
-- DROP TABLE IF EXISTS matrix_events;
