CREATE INDEX IF NOT EXISTS idx_browser_ingest_valid_content_progress
    ON browser_ingest_events (platform, created_at DESC)
    WHERE endpoint <> 'browser_heartbeat'
      AND (
        observed_count > 0
        OR stored_count > 0
        OR (
          metadata ? 'probe_reason'
          AND COALESCE(metadata->>'probe_reason', '')
              NOT IN ('manual_backend_probe', 'forced_recovery_started')
        )
      );
