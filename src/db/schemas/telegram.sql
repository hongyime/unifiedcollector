CREATE TABLE IF NOT EXISTS telegram_chats (
    chat_id BIGINT PRIMARY KEY,
    title VARCHAR(500),
    username VARCHAR(255),
    chat_type VARCHAR(20),
    member_count INT,
    last_scraped TIMESTAMP,
    spider_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tg_chats_status ON telegram_chats(spider_status);
