-- Source-owned generic-extractor jobs.  Never store expiring CDN asset URLs.
CREATE TABLE IF NOT EXISTS extractor_fallback_queue (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('profile', 'post', 'embed')),
    content_id text NOT NULL,
    target_url text NOT NULL,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'retry', 'done', 'terminal')),
    next_retry_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz,
    finished_at timestamptz,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, kind, content_id)
);
CREATE INDEX IF NOT EXISTS idx_extractor_fallback_queue_claim
    ON extractor_fallback_queue (source, next_retry_at, id)
    WHERE status IN ('pending', 'retry');
