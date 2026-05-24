-- Telegram V2 Schema

CREATE TABLE IF NOT EXISTS telegram_chats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_chat_id VARCHAR(255) UNIQUE NOT NULL,
    title VARCHAR(500),
    username VARCHAR(255),
    type VARCHAR(20), -- 'channel', 'group', 'supergroup'
    description TEXT,
    members_count INTEGER DEFAULT 0,
    collected_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_chat_telegram UNIQUE (platform_chat_id)
);

CREATE TABLE IF NOT EXISTS telegram_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(255),
    first_name VARCHAR(255),
    last_name VARCHAR(255),
    phone VARCHAR(50),
    bio TEXT,
    photo_url TEXT,
    collected_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_user_telegram UNIQUE (platform_user_id)
);

CREATE TABLE IF NOT EXISTS telegram_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_message_id VARCHAR(255) UNIQUE NOT NULL,
    chat_id UUID REFERENCES telegram_chats(id) ON DELETE CASCADE,
    sender_id UUID REFERENCES telegram_users(id) ON DELETE SET NULL,
    text TEXT,
    caption TEXT, -- For media with caption
    media_type VARCHAR(50), -- 'photo', 'video', 'document', 'audio', 'voice', 'sticker', 'location', 'contact', null
    media_file_id VARCHAR(255),
    reply_to_message_id VARCHAR(255), -- References platform_message_id
    is_edited BOOLEAN DEFAULT FALSE,
    edit_date TIMESTAMP,
    forward_from_chat_id VARCHAR(255),
    forward_from_message_id VARCHAR(255),
    via_bot_id VARCHAR(255),
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB,
    CONSTRAINT unique_platform_message_telegram UNIQUE (platform_message_id)
);

CREATE TABLE IF NOT EXISTS telegram_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_chat_id VARCHAR(255) UNIQUE NOT NULL,
    title VARCHAR(500),
    source VARCHAR(50), -- 'member_list', 'mentioned', 'manual'
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_spider_chat_telegram UNIQUE (platform_chat_id)
);

CREATE INDEX IF NOT EXISTS idx_tg_messages_chat ON telegram_messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_tg_messages_sender ON telegram_messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_tg_spider_status ON telegram_spider_queue(status);
