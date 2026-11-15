CREATE TABLE IF NOT EXISTS whatsapp_group_members (
    platform_chat_id  VARCHAR(255) NOT NULL REFERENCES whatsapp_chats(platform_chat_id) ON DELETE CASCADE,
    participant_jid   VARCHAR(255) NOT NULL,
    role              VARCHAR(20) NOT NULL DEFAULT 'member',
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    removed_at        TIMESTAMPTZ,
    PRIMARY KEY (platform_chat_id, participant_jid)
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_group_members_participant
    ON whatsapp_group_members(participant_jid) WHERE is_active;
