-- Audit Telegram invite/public-link join/scrape/leave attempts.

CREATE TABLE IF NOT EXISTS telegram_invite_visits (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_link_id      INTEGER REFERENCES discovered_links(id) ON DELETE SET NULL,
    url                 TEXT NOT NULL,
    invite_hash         TEXT,
    username            TEXT,
    resolved_chat_id    UUID REFERENCES telegram_chats(id) ON DELETE SET NULL,
    account_name        TEXT,
    joined_this_pass    BOOLEAN NOT NULL DEFAULT false,
    joined_at           TIMESTAMPTZ,
    left_at             TIMESTAMPTZ,
    members_collected   INTEGER NOT NULL DEFAULT 0,
    messages_collected  INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,
    error               TEXT,
    collected_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_telegram_invite_visits_link
    ON telegram_invite_visits (source_link_id, collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_telegram_invite_visits_status
    ON telegram_invite_visits (status, collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_telegram_invite_visits_hash
    ON telegram_invite_visits (invite_hash);

CREATE INDEX IF NOT EXISTS idx_telegram_invite_visits_username
    ON telegram_invite_visits (lower(username));
