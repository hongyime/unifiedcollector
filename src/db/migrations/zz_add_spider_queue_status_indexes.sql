-- 2026-07-17: support dashboard backfill-equilibrium queue metrics.
-- Idempotent indexes for status-count/group-by queries across source-specific
-- spider queues. On the live GitHub queue the index is created concurrently
-- before this migration is recorded, so this file no-ops there.

SET lock_timeout = '2s';

CREATE INDEX IF NOT EXISTS idx_github_spider_queue_status
  ON github_spider_queue(status);

CREATE INDEX IF NOT EXISTS idx_lemon8_spider_queue_status
  ON lemon8_spider_queue(status);

CREATE INDEX IF NOT EXISTS idx_strava_spider_queue_status
  ON strava_spider_queue(status);

CREATE INDEX IF NOT EXISTS idx_tiktok_spider_queue_status
  ON tiktok_spider_queue(status);

CREATE INDEX IF NOT EXISTS idx_youtube_spider_queue_status
  ON youtube_spider_queue(status);
