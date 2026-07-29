-- Columns used by the WhatsApp dashboard/API on databases created before
-- the current checked-in base schema caught up.

ALTER TABLE whatsapp_chats
    ADD COLUMN IF NOT EXISTS chat_type TEXT;

UPDATE whatsapp_chats
SET chat_type = CASE
    WHEN platform_chat_id LIKE '%@g.us' THEN 'group'
    WHEN platform_chat_id LIKE '%@newsletter' THEN 'channel'
    WHEN platform_chat_id LIKE '%@broadcast' THEN 'broadcast'
    ELSE 'dm'
END
WHERE chat_type IS NULL;

ALTER TABLE whatsapp_chats
    ALTER COLUMN chat_type SET DEFAULT 'dm';

ALTER TABLE whatsapp_users
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
