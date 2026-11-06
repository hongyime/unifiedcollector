-- Canonical inbound WhatsApp interaction state. Poll vote updates are encrypted
-- by the provider; this stores only poll definitions received in the message stream.
CREATE TABLE IF NOT EXISTS whatsapp_reactions (
    platform_chat_id VARCHAR(255) NOT NULL,
    target_message_id VARCHAR(255) NOT NULL,
    actor_jid VARCHAR(255) NOT NULL,
    emoji TEXT,
    reaction_message_id VARCHAR(255),
    reacted_at TIMESTAMPTZ,
    removed_at TIMESTAMPTZ,
    session_name VARCHAR(255),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (platform_chat_id, target_message_id, actor_jid)
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_reactions_target
    ON whatsapp_reactions (target_message_id);

CREATE TABLE IF NOT EXISTS whatsapp_polls (
    platform_message_id VARCHAR(255) PRIMARY KEY,
    platform_chat_id VARCHAR(255) NOT NULL,
    creator_jid VARCHAR(255),
    question TEXT,
    options JSONB NOT NULL DEFAULT '[]'::jsonb,
    selectable_options_count INTEGER,
    poll_type VARCHAR(32) NOT NULL DEFAULT 'poll',
    created_at TIMESTAMPTZ,
    session_name VARCHAR(255),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_polls_chat
    ON whatsapp_polls (platform_chat_id, created_at DESC);
