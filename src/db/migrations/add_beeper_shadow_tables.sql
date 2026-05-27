-- Beeper Desktop Local API shadow ingest tables.
--
-- Polymorphic across all networks Beeper bridges (Telegram, WhatsApp, Discord,
-- Signal, LinkedIn, Facebook, Google Chat, Instagram, Slack, iMessage, native
-- Matrix, etc). Network is identified by `account_id` + `network` columns.
--
-- This is the Option-A redundancy path agreed in Wave 4:
--   first-party collectors  -> {telegram_*, whatsapp_*, instagram_*, ...} tables
--   beeper shadow path      -> beeper_shadow_* tables (THIS MIGRATION)
-- No dedupe/reconciler layer between them — parallel writes by design.
--
-- Source: GET /v1/accounts, /v1/chats, /v1/chats/{id}/messages on the Beeper
-- Desktop Local API (default 127.0.0.1:23373). Field names mirror the live
-- response shape probed 2026-05-27.

CREATE TABLE IF NOT EXISTS beeper_shadow_chats (
    chat_id           TEXT PRIMARY KEY,           -- Matrix room id, e.g. "!IwJ...:beeper.local"
    local_chat_id     TEXT,                        -- Beeper-internal int id (string-typed)
    account_id        TEXT NOT NULL,               -- "discordgo", "telegram", "whatsapp", "matrix", ...
    network           TEXT NOT NULL,               -- "Discord", "Telegram", "WhatsApp", "Beeper", ...
    title             TEXT,
    description       TEXT,
    img_url           TEXT,                        -- mxc:// or file:// path Beeper resolved locally
    chat_type         TEXT,                        -- "group", "single", "channel", "broadcast"
    is_read_only      BOOLEAN DEFAULT FALSE,
    is_unread         BOOLEAN,
    is_archived       BOOLEAN,
    is_muted          BOOLEAN,
    is_low_priority   BOOLEAN,
    last_message_ts   TIMESTAMPTZ,
    raw               JSONB NOT NULL,              -- full chat object as returned
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS beeper_shadow_chats_network_idx ON beeper_shadow_chats (network);
CREATE INDEX IF NOT EXISTS beeper_shadow_chats_account_idx ON beeper_shadow_chats (account_id);
CREATE INDEX IF NOT EXISTS beeper_shadow_chats_last_seen_idx ON beeper_shadow_chats (last_seen_at DESC);

CREATE TABLE IF NOT EXISTS beeper_shadow_messages (
    message_id        TEXT NOT NULL,               -- Beeper message id (per-network opaque)
    chat_id           TEXT NOT NULL REFERENCES beeper_shadow_chats(chat_id) ON DELETE CASCADE,
    account_id        TEXT NOT NULL,
    network           TEXT NOT NULL,
    sender_id         TEXT,                        -- Matrix-style "@handle:beeper.local"
    sender_name       TEXT,
    is_sender         BOOLEAN,                     -- true => message FROM the logged-in account
    timestamp         TIMESTAMPTZ NOT NULL,
    sort_key          TEXT,                        -- Beeper internal ordering hint
    msg_type          TEXT,                        -- "TEXT", "IMAGE", "FILE", "STICKER", "AUDIO", ...
    text              TEXT,
    is_deleted        BOOLEAN DEFAULT FALSE,
    is_unread         BOOLEAN,
    mentions          JSONB,                       -- array of mention objects
    seen              JSONB,                        -- {participant_id: timestamp} read receipts
    reply_to_id       TEXT,
    edited_at         TIMESTAMPTZ,
    attachments       JSONB,                       -- array (file/image/audio/video items)
    reactions         JSONB,                       -- array (emoji + participant id list)
    raw               JSONB NOT NULL,              -- full message object
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS beeper_shadow_messages_chat_ts_idx
    ON beeper_shadow_messages (chat_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS beeper_shadow_messages_network_ts_idx
    ON beeper_shadow_messages (network, timestamp DESC);
CREATE INDEX IF NOT EXISTS beeper_shadow_messages_sender_idx
    ON beeper_shadow_messages (sender_id);
CREATE INDEX IF NOT EXISTS beeper_shadow_messages_text_fts_idx
    ON beeper_shadow_messages USING gin (to_tsvector('simple', coalesce(text, '')));

CREATE TABLE IF NOT EXISTS beeper_shadow_participants (
    chat_id           TEXT NOT NULL REFERENCES beeper_shadow_chats(chat_id) ON DELETE CASCADE,
    participant_id    TEXT NOT NULL,               -- "@discordgo_1234:beeper.local"
    network           TEXT NOT NULL,
    username          TEXT,
    full_name         TEXT,
    img_url           TEXT,
    is_self           BOOLEAN DEFAULT FALSE,
    is_admin          BOOLEAN DEFAULT FALSE,
    is_pending        BOOLEAN DEFAULT FALSE,
    is_network_bot    BOOLEAN DEFAULT FALSE,
    cannot_message    BOOLEAN DEFAULT FALSE,
    raw               JSONB NOT NULL,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, participant_id)
);

CREATE INDEX IF NOT EXISTS beeper_shadow_participants_id_idx
    ON beeper_shadow_participants (participant_id);
CREATE INDEX IF NOT EXISTS beeper_shadow_participants_network_idx
    ON beeper_shadow_participants (network);

-- Per-chat sync cursor for incremental backfill via /v1/chats/{id}/messages?cursor=X
CREATE TABLE IF NOT EXISTS beeper_shadow_sync_state (
    chat_id              TEXT PRIMARY KEY REFERENCES beeper_shadow_chats(chat_id) ON DELETE CASCADE,
    oldest_cursor        TEXT,                     -- walk-backwards cursor (for backfill)
    newest_cursor        TEXT,                     -- walk-forwards cursor (for incremental tail)
    backfill_complete    BOOLEAN NOT NULL DEFAULT FALSE,
    last_synced_at       TIMESTAMPTZ,
    last_message_ts      TIMESTAMPTZ,
    error_count          INT NOT NULL DEFAULT 0,
    last_error           TEXT
);

CREATE INDEX IF NOT EXISTS beeper_shadow_sync_state_synced_idx
    ON beeper_shadow_sync_state (last_synced_at);

-- Account-level state (one row per connected Beeper account)
CREATE TABLE IF NOT EXISTS beeper_shadow_accounts (
    account_id        TEXT PRIMARY KEY,            -- "telegram", "discordgo", ...
    network           TEXT NOT NULL,
    login_id          TEXT,
    bridge_type       TEXT,
    bridge_provider   TEXT,                        -- "cloud" or "local"
    user_id           TEXT,
    user_full_name    TEXT,
    user_username     TEXT,
    user_email        TEXT,
    user_phone        TEXT,
    img_url           TEXT,
    status            TEXT,                        -- "connected", "disconnected", ...
    raw               JSONB NOT NULL,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE beeper_shadow_chats IS 'Polymorphic chat metadata from Beeper Desktop Local API (Option-A redundancy path)';
COMMENT ON TABLE beeper_shadow_messages IS 'Polymorphic message stream from Beeper bridges (parallel to first-party collector tables)';
COMMENT ON TABLE beeper_shadow_participants IS 'Per-chat participant rows from Beeper Desktop Local API';
COMMENT ON TABLE beeper_shadow_sync_state IS 'Per-chat backfill + tail cursor state for Beeper sync loop';
COMMENT ON TABLE beeper_shadow_accounts IS 'Connected Beeper accounts (one row per network)';
