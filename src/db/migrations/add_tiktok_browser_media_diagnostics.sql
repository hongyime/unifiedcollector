CREATE TABLE IF NOT EXISTS tiktok_browser_media_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content_id TEXT NOT NULL,
    username TEXT,
    source_url TEXT,
    url_hash TEXT NOT NULL,
    asset_role TEXT,
    content_type TEXT,
    width INTEGER,
    height INTEGER,
    file_size BIGINT,
    mime_type TEXT,
    extension_version TEXT,
    ingest_mode TEXT NOT NULL DEFAULT 'url',
    outcome TEXT NOT NULL DEFAULT 'observed',
    reason TEXT,
    needs_revisit BOOLEAN NOT NULL DEFAULT false,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tiktok_browser_media_candidate
    ON tiktok_browser_media_candidates (content_id, url_hash, ingest_mode);

CREATE INDEX IF NOT EXISTS idx_tiktok_browser_media_candidates_seen
    ON tiktok_browser_media_candidates (last_seen DESC);

CREATE INDEX IF NOT EXISTS idx_tiktok_browser_media_candidates_outcome
    ON tiktok_browser_media_candidates (outcome, last_seen DESC);

CREATE INDEX IF NOT EXISTS idx_tiktok_browser_media_candidates_revisit
    ON tiktok_browser_media_candidates (needs_revisit, last_seen DESC)
    WHERE needs_revisit;

CREATE TABLE IF NOT EXISTS tiktok_browser_revisit_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content_id TEXT NOT NULL,
    username TEXT,
    post_url TEXT,
    source_url TEXT,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 50,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_visit_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tiktok_browser_revisit_content
    ON tiktok_browser_revisit_queue (content_id);

CREATE INDEX IF NOT EXISTS idx_tiktok_browser_revisit_due
    ON tiktok_browser_revisit_queue (status, next_visit_at, priority DESC);
