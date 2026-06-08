-- Add phone_number and is_business columns to whatsapp_users
ALTER TABLE whatsapp_users ADD COLUMN IF NOT EXISTS phone_number VARCHAR(20);
ALTER TABLE whatsapp_users ADD COLUMN IF NOT EXISTS is_business BOOLEAN DEFAULT FALSE;

-- Backfill phone_number from existing platform_user_id (JID format: 6512345678@s.whatsapp.net)
UPDATE whatsapp_users
SET phone_number = split_part(platform_user_id, '@', 1)
WHERE phone_number IS NULL
  AND platform_user_id LIKE '%@s.whatsapp.net';
