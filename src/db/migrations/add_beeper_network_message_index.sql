CREATE INDEX IF NOT EXISTS idx_beeper_messages_network_message
    ON beeper_shadow_messages (network, message_id);

CREATE INDEX IF NOT EXISTS idx_beeper_messages_network_sender
    ON beeper_shadow_messages (network, sender_id);
