-- WhatsApp V2 Schema

CREATE TABLE IF NOT EXISTS whatsapp_chats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_chat_id VARCHAR(255) UNIQUE NOT NULL, -- jid
    name VARCHAR(500),
    is_group BOOLEAN DEFAULT FALSE,
    chat_type TEXT DEFAULT 'dm',
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
    phone_number VARCHAR(20),
    is_business BOOLEAN DEFAULT FALSE,
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

CREATE INDEX IF NOT EXISTS idx_wa_messages_chat ON whatsapp_messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_wa_messages_sender ON whatsapp_messages(sender_id);

-- LID → phone JID mapping table. Populated from contacts.update events when
-- Baileys provides contact.lid on @s.whatsapp.net contacts. Used by
-- _track_user_profile() to resolve group message senders from @lid to
-- phone-based JIDs so the analyzer's entity_platform_links lookups work.
CREATE TABLE IF NOT EXISTS whatsapp_lid_map (
    lid VARCHAR(255) PRIMARY KEY,          -- e.g. 57703036666033@lid
    phone_jid VARCHAR(255) NOT NULL,       -- e.g. 6512345678@s.whatsapp.net
    display_name VARCHAR(255),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wa_lid_map_phone ON whatsapp_lid_map(phone_jid);
