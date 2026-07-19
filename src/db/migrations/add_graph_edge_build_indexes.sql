-- Support WhatsApp social graph edge derivation without seq-scanning all messages.
CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_chat_sender
    ON whatsapp_messages(chat_id, sender_id)
    WHERE sender_id IS NOT NULL;
