-- 2026-07-06 (Option B of #39): TikTok DM tables, mirroring instagram_dm.
--
-- Schema derived empirically from 24 real-message samples captured through
-- the browser extension's passive WS-hook (v1.21.7+ SAMPLE_MAX=200) on
-- wss://im-ws-sg.tiktok.com/ws/v2. See tmp/dm_analysis/ for the analyzer
-- scripts + sample teardown. Field-number mapping:
--
--   outer envelope (proto):  1=opaque, 2=ts_ns, 3=method (5=user event),
--                            8=nested envelope
--   nested envelope:         1=code (500), 6=wrapper
--   wrapper:                 500=message envelope
--   message envelope:        2=conversation_id
--                            5=message entry [
--                              1=conversation_id, 3=message_id (uint64),
--                              6=message_type (7=text), 7=sender_uid,
--                              8=content_json ({aweType, text, ...}),
--                              10=create_time_ms, 14=sender_secuid ]
--
-- We ship the decoded structure to POST /social/dm-decoded which upserts
-- into these tables. Raw sample capture continues in parallel as a schema-
-- drift canary (see /tmp/dm_samples/ + P1.1 rotation).

CREATE TABLE IF NOT EXISTS tiktok_dm_thread (
    conversation_id   TEXT PRIMARY KEY,
    conversation_type INTEGER,                 -- 1=1:1, other values TBD (group?)
    participants      TEXT[],                  -- parsed from '0:1:UID_A:UID_B'
    owner_account     TEXT,                    -- device_id / owner uid from WS URL
    last_activity     TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- awe_type is snake_case for consistency with the rest of the codebase's SQL
-- (media_items.file_hash, whatsapp_messages.chat_jid, ...). The wire field
-- inside content_json is TikTok's "aweType" camelCase — the decoder in
-- extension/inject.js reads content_json.aweType and maps to this column.
CREATE TABLE IF NOT EXISTS tiktok_dm (
    message_id        TEXT PRIMARY KEY,          -- inner field 5.3, kept as text (uint64)
    conversation_id   TEXT NOT NULL,             -- inner field 5.1
    sender_uid        TEXT,                      -- inner field 5.7
    sender_secuid     TEXT,                      -- inner field 5.14
    text              TEXT,                      -- content_json.text (aweType=0 only)
    awe_type          INTEGER,                   -- content_json.aweType
    message_type      INTEGER,                   -- inner field 5.6 (7=text, others TBD)
    "timestamp"       TIMESTAMPTZ,               -- inner field 5.10 (ms) converted
    is_from_me        BOOLEAN NOT NULL DEFAULT false,
    owner_account     TEXT,                      -- who's logged in (device_id / owner uid)
    client_message_id TEXT,                      -- inner field 5.9[s:client_message_id]
    is_stranger       BOOLEAN,                   -- inner field 5.9[s:is_stranger]
    raw_content       JSONB,                     -- content_json in full
    collected_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tt_dm_conversation ON tiktok_dm(conversation_id, "timestamp");
CREATE INDEX IF NOT EXISTS idx_tt_dm_owner        ON tiktok_dm(owner_account);
CREATE INDEX IF NOT EXISTS idx_tt_dm_sender       ON tiktok_dm(sender_uid);
