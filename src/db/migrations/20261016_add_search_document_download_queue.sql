CREATE TABLE IF NOT EXISTS search_document_download_queue (
    id BIGSERIAL PRIMARY KEY,
    url_hash BYTEA NOT NULL UNIQUE,
    source_url TEXT NOT NULL,
    search_query TEXT NOT NULL DEFAULT '',
    title TEXT,
    snippet TEXT,
    rank INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'failed', 'completed')),
    attempts SMALLINT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claim_expires_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_search_document_download_queue_due
    ON search_document_download_queue (next_attempt_at, id)
    WHERE status IN ('pending', 'claimed');

CREATE INDEX IF NOT EXISTS idx_search_documents_source_url
    ON search_documents (source_url);
