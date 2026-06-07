-- WhatsApp discovered links — URLs extracted from messages for the dashboard.

CREATE TABLE IF NOT EXISTS wa_discovered_links (
    id              SERIAL PRIMARY KEY,
    chat_id         UUID REFERENCES whatsapp_chats(id) ON DELETE CASCADE,
    message_id      UUID REFERENCES whatsapp_messages(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,
    domain          TEXT,
    link_type       TEXT,                   -- article, image, video, social, other
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending, fetched, error
    title           TEXT,
    description     TEXT,
    thumbnail_url   TEXT,
    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fetched_at      TIMESTAMPTZ,
    metadata        JSONB
);

CREATE INDEX IF NOT EXISTS idx_wa_links_chat ON wa_discovered_links (chat_id);
CREATE INDEX IF NOT EXISTS idx_wa_links_type ON wa_discovered_links (link_type);
CREATE INDEX IF NOT EXISTS idx_wa_links_status ON wa_discovered_links (status);
CREATE INDEX IF NOT EXISTS idx_wa_links_discovered ON wa_discovered_links (discovered_at DESC);
