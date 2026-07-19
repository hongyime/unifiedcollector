-- Speed up dashboard and Telegram heartbeat hourly ingestion checks.
CREATE INDEX IF NOT EXISTS idx_instagram_posts_collected ON instagram_posts(collected_at);
CREATE INDEX IF NOT EXISTS idx_tiktok_posts_collected ON tiktok_posts(collected_at);
CREATE INDEX IF NOT EXISTS idx_lemon8_posts_collected ON lemon8_posts(collected_at);
CREATE INDEX IF NOT EXISTS idx_threads_posts_collected ON threads_posts(collected_at);
CREATE INDEX IF NOT EXISTS idx_facebook_posts_collected ON facebook_posts(collected_at);
CREATE INDEX IF NOT EXISTS idx_x_posts_collected ON x_posts(collected_at);
