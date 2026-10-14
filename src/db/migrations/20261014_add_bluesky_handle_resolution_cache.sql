-- Cache positive and negative handle lookups separately from the probe queue.
-- A cached miss is never enqueued for profile probing.
CREATE TABLE IF NOT EXISTS bluesky_handle_resolution_cache (
    handle      TEXT PRIMARY KEY,
    did         TEXT,
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bluesky_handle_resolution_cache_resolved_at
    ON bluesky_handle_resolution_cache (resolved_at);
