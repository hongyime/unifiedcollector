-- 2026-06-27: deletion / revoke tracking for the analyzer's "what changed" (B).
-- Capture WHEN a message was deleted, not just THAT it was. Idempotent.
-- telegram_messages uses metadata->>'deleted' / metadata->>'deleted_at' (jsonb) —
-- no column needed there. WhatsApp + Beeper get real columns.
ALTER TABLE whatsapp_messages      ADD COLUMN IF NOT EXISTS is_deleted boolean DEFAULT false;
ALTER TABLE whatsapp_messages      ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE beeper_shadow_messages ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_deleted ON whatsapp_messages(is_deleted) WHERE is_deleted;
CREATE INDEX IF NOT EXISTS idx_beeper_msgs_deleted ON beeper_shadow_messages(is_deleted) WHERE is_deleted;
