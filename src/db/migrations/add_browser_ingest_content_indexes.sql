CREATE INDEX IF NOT EXISTS idx_browser_ingest_heartbeat_platform_created
    ON browser_ingest_events (platform, created_at DESC)
    WHERE endpoint = 'browser_heartbeat';

CREATE INDEX IF NOT EXISTS idx_browser_ingest_content_platform_created
    ON browser_ingest_events (platform, created_at DESC)
    WHERE endpoint <> 'browser_heartbeat'
      AND (observed_count > 0 OR stored_count > 0);
