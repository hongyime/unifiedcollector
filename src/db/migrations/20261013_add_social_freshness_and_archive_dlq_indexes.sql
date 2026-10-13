-- Ensure watchdog freshness lookups can seek by source before ordering by time.
-- add_media_source_stats_index.sql created this index already; this idempotent
-- declaration also repairs databases where the physical index was removed.
CREATE INDEX IF NOT EXISTS idx_media_source_stats
    ON media_items (source, collected_at DESC)
    INCLUDE (file_size);

-- Raw-archive success clears only matching active failures. Keep those deletes
-- selective without indexing unrelated DLQ rows or error-message contents.
CREATE INDEX IF NOT EXISTS idx_dlq_browser_raw_archive_cleanup
    ON dead_letter_queue (source, entity_id)
    WHERE status IN ('pending', 'in_progress')
      AND error_message LIKE 'browser raw archive failed:%';
