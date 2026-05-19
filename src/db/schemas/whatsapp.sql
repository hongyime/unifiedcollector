CREATE TABLE IF NOT EXISTS whatsapp_chats (
    chat_id VARCHAR(100) PRIMARY KEY,
    chat_name VARCHAR(500) NOT NULL,
    is_group BOOLEAN DEFAULT FALSE,
    participant_count INT,
    last_scraped TIMESTAMP,
    spider_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_wa_chats_status ON whatsapp_chats(spider_status);
