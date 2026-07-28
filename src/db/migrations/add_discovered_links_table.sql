-- Generic Tier 6 URL discovery across collector sources.
-- Source-specific tables such as wa_discovered_links can remain for rich
-- platform views; this table gives analyzer/rebuild/spider flows one durable
-- place to find links extracted from captions, messages, and descriptions.

CREATE TABLE IF NOT EXISTS discovered_links (
    id               SERIAL PRIMARY KEY,
    source           VARCHAR(50) NOT NULL,
    source_table     VARCHAR(100),
    source_record_id TEXT NOT NULL,
    context_id       TEXT,
    entity_id        TEXT,
    url              TEXT NOT NULL,
    domain           TEXT,
    link_type        TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    title            TEXT,
    description      TEXT,
    discovered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fetched_at       TIMESTAMPTZ,
    metadata         JSONB,
    UNIQUE (source, source_record_id, url)
);

CREATE INDEX IF NOT EXISTS idx_discovered_links_source
    ON discovered_links (source, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovered_links_domain
    ON discovered_links (domain);
CREATE INDEX IF NOT EXISTS idx_discovered_links_status
    ON discovered_links (status, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovered_links_context
    ON discovered_links (source, context_id);
