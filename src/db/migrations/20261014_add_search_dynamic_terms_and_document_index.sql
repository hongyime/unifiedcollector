CREATE TABLE IF NOT EXISTS search_dynamic_terms (
    term TEXT PRIMARY KEY CHECK (length(term) BETWEEN 2 AND 80),
    term_kind TEXT NOT NULL CHECK (term_kind IN ('username', 'keyword', 'hashtag')),
    source_platform TEXT NOT NULL,
    source_reference TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    next_search_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_searched_at TIMESTAMPTZ,
    search_count INTEGER NOT NULL DEFAULT 0 CHECK (search_count >= 0),
    template_cursor SMALLINT NOT NULL DEFAULT 0 CHECK (template_cursor BETWEEN 0 AND 6),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_search_dynamic_terms_due
    ON search_dynamic_terms (next_search_at, expires_at, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS search_document_index (
    media_item_id UUID PRIMARY KEY REFERENCES media_items(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    title TEXT,
    file_extension VARCHAR(20) NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    content_text TEXT NOT NULL DEFAULT '',
    extraction_method VARCHAR(40) NOT NULL,
    page_count INTEGER,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    search_vector TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig, coalesce(title, '') || ' ' || content_text)
    ) STORED
);

CREATE INDEX IF NOT EXISTS idx_search_document_index_vector
    ON search_document_index USING GIN (search_vector);

CREATE INDEX IF NOT EXISTS idx_search_document_index_sha256
    ON search_document_index (sha256);
