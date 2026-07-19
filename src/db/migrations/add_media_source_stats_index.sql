CREATE INDEX IF NOT EXISTS idx_media_source_stats
    ON media_items (source, collected_at DESC)
    INCLUDE (file_size);
