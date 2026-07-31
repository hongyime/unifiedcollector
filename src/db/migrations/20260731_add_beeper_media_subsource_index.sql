CREATE INDEX IF NOT EXISTS idx_media_beeper_subsource_stats
  ON media_items (collected_at DESC)
  INCLUDE (file_size, entity_id, metadata)
  WHERE source = 'beeper';
