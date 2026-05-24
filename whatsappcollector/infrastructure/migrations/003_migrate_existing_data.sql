-- Migration 003 — Copy existing public schema data into collector schema
-- Run AFTER 002_schema_per_service.sql, BEFORE retiring processor-py.
-- Safe to run while processor-py is still writing to the public schema —
-- use INSERT ... ON CONFLICT DO NOTHING to avoid duplicate key errors.
--
-- Order matters: parent tables (wa_sessions, chats, users) before child tables.

-- -------------------------------------------------------------------------
-- wa_sessions (public → collector)
-- -------------------------------------------------------------------------
INSERT INTO collector.wa_sessions (session_name, status, created_at)
SELECT session_name, status, created_at
FROM wa_sessions
ON CONFLICT (session_name) DO NOTHING;

-- -------------------------------------------------------------------------
-- chats (public → collector)
-- -------------------------------------------------------------------------
INSERT INTO collector.chats (jid, chat_type, name, collected_at)
SELECT jid, COALESCE(chat_type, 'dm'), subject, created_at
FROM chats
ON CONFLICT (jid) DO NOTHING;

-- -------------------------------------------------------------------------
-- users (public → collector)
-- public.users has separate UNIQUE on jid and lid — collector.users has jid PK only
-- -------------------------------------------------------------------------
INSERT INTO collector.users (jid, display_name, first_seen, last_seen)
SELECT jid, display_name, created_at, updated_at
FROM users
WHERE jid IS NOT NULL
ON CONFLICT (jid) DO NOTHING;

-- -------------------------------------------------------------------------
-- jid_lid_map (public → collector)
-- public.jid_lid_map has PK (jid, lid); collector.jid_lid_map has PK (jid, session_name)
-- We cannot recover session_name from old data, so use 'legacy' as placeholder.
-- -------------------------------------------------------------------------
INSERT INTO collector.jid_lid_map (jid, lid, session_name, mapped_at)
SELECT jid, lid, 'legacy', resolved_at
FROM jid_lid_map
ON CONFLICT (jid, session_name) DO NOTHING;

-- -------------------------------------------------------------------------
-- messages (public → collector.raw_messages)
-- public.messages has TEXT PRIMARY KEY on message_id only (BUG-01 present).
-- We map old records in; UNIQUE (message_id, chat_jid) enforced in collector.
-- session_name is unknown for legacy records — use 'legacy'.
-- raw_payload may be NULL in old table; substitute empty JSONB object.
-- -------------------------------------------------------------------------
INSERT INTO collector.raw_messages (
    message_id, chat_jid, sender_jid, sender_lid,
    session_name, message_type, body, is_forwarded, forwarding_score,
    quoted_msg_id, collected_at, raw_payload
)
SELECT
    message_id,
    chat_jid,
    sender_jid,
    sender_lid,
    'legacy',
    message_type,
    body,
    COALESCE(is_forwarded, FALSE),
    COALESCE(forward_score, 0),
    quoted_msg_id,
    COALESCE(created_at, NOW()),
    COALESCE(raw_payload, '{}'::jsonb)
FROM messages
ON CONFLICT (message_id, chat_jid) DO NOTHING;

-- -------------------------------------------------------------------------
-- group_participants (public → collector)
-- -------------------------------------------------------------------------
INSERT INTO collector.group_participants (chat_jid, user_jid, role, seen_at)
SELECT group_jid, user_jid, role, added_at
FROM group_participants
ON CONFLICT (chat_jid, user_jid) DO NOTHING;

-- -------------------------------------------------------------------------
-- calls (public → collector)
-- -------------------------------------------------------------------------
INSERT INTO collector.calls (call_id, from_jid, call_type, status, session_name, occurred_at)
SELECT call_id, from_jid,
    CASE WHEN is_video THEN 'video' ELSE 'voice' END,
    status,
    COALESCE(session_name, 'legacy'),
    call_date
FROM calls
ON CONFLICT DO NOTHING;

-- -------------------------------------------------------------------------
-- media → media_archival.media_files
-- The old media table maps 1:1 to messages; translate to new structure.
-- -------------------------------------------------------------------------
INSERT INTO media_archival.media_files (
    message_id, chat_jid, mime_type, file_size_bytes,
    by_id_path, sha256,
    download_status, downloaded_at, collected_at, expiry_at
)
SELECT
    m.message_id,
    msg.chat_jid,
    m.mime_type,
    m.file_size,
    m.local_path,
    m.file_sha256,
    CASE WHEN m.is_downloaded THEN 'complete' ELSE 'pending' END,
    CASE WHEN m.is_downloaded THEN m.created_at ELSE NULL END,
    m.created_at,
    m.expires_at
FROM media m
JOIN messages msg ON msg.message_id = m.message_id
ON CONFLICT (message_id, chat_jid) DO NOTHING;
