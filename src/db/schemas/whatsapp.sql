-- WhatsApp V2 Schema

CREATE TABLE IF NOT EXISTS whatsapp_chats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_chat_id VARCHAR(255) UNIQUE NOT NULL, -- jid
    name VARCHAR(500),
    is_group BOOLEAN DEFAULT FALSE,
    participant_count INTEGER DEFAULT 0,
    description TEXT,
    collected_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_chat_whatsapp UNIQUE (platform_chat_id)
);

CREATE TABLE IF NOT EXISTS whatsapp_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id VARCHAR(255) UNIQUE NOT NULL, -- jid
    name VARCHAR(255),
    pushname VARCHAR(255),
    status TEXT,
    photo_url TEXT,
    about TEXT,
    collected_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_user_whatsapp UNIQUE (platform_user_id)
);

CREATE TABLE IF NOT EXISTS whatsapp_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_message_id VARCHAR(255) UNIQUE NOT NULL,
    chat_id UUID REFERENCES whatsapp_chats(id) ON DELETE CASCADE,
    sender_id UUID REFERENCES whatsapp_users(id) ON DELETE SET NULL,
    from_me BOOLEAN DEFAULT FALSE,
    text TEXT,
    media_url TEXT,
    media_mime_type VARCHAR(100),
    media_size INTEGER,
    thumbnail_url TEXT,
    quoted_message_id VARCHAR(255),
    quoted_text TEXT,
    forward_from_name VARCHAR(255),
    timestamp TIMESTAMP,
    status VARCHAR(50), -- 'pending', 'delivered', 'read', 'error'
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB,
    CONSTRAINT unique_platform_message_whatsapp UNIQUE (platform_message_id)
);

-- Face Recognition (from whatsapp_ext.sql)
CREATE TABLE IF NOT EXISTS wa_face_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label TEXT UNIQUE,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wa_face_embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id UUID REFERENCES wa_face_identities(id) ON DELETE SET NULL,
    embedding vector(128),
    source_media_id UUID REFERENCES media_items(id) ON DELETE CASCADE,
    face_box JSONB, -- [top, right, bottom, left]
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wa_messages_chat ON whatsapp_messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_wa_messages_sender ON whatsapp_messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_wa_face_identity ON wa_face_embeddings(identity_id);
