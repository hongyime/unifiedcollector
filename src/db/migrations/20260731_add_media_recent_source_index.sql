CREATE INDEX IF NOT EXISTS idx_media_collected_source_recent_stats
  ON media_items (collected_at DESC, source)
  INCLUDE (file_size);
