CREATE INDEX IF NOT EXISTS idx_beeper_shadow_attachment_chat_ts
    ON beeper_shadow_messages (chat_id, timestamp DESC NULLS LAST)
    WHERE attachments IS NOT NULL
      AND attachments <> '[]'::jsonb;
