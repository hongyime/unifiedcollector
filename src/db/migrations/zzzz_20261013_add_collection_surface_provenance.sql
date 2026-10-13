-- Canonical feed provenance for post/media writes and browser capture progress.
ALTER TABLE media_items
    ADD COLUMN IF NOT EXISTS collection_surface TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE instagram_posts
    ADD COLUMN IF NOT EXISTS collection_surface TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE tiktok_posts
    ADD COLUMN IF NOT EXISTS collection_surface TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE x_posts
    ADD COLUMN IF NOT EXISTS collection_surface TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE threads_posts
    ADD COLUMN IF NOT EXISTS collection_surface TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE facebook_posts
    ADD COLUMN IF NOT EXISTS collection_surface TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE lemon8_posts
    ADD COLUMN IF NOT EXISTS collection_surface TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE strava_activities
    ADD COLUMN IF NOT EXISTS collection_surface TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE youtube_community_posts
    ADD COLUMN IF NOT EXISTS collection_surface TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE telegram_messages
    ADD COLUMN IF NOT EXISTS collection_surface TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE whatsapp_messages
    ADD COLUMN IF NOT EXISTS collection_surface TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE browser_ingest_events
    ADD COLUMN IF NOT EXISTS collection_surface TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE browser_ingest_events
    ADD COLUMN IF NOT EXISTS deduped_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE browser_ingest_events
    ADD COLUMN IF NOT EXISTS failed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE browser_ingest_events
    ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE browser_ingest_events
    ADD COLUMN IF NOT EXISTS error_class TEXT;

CREATE INDEX IF NOT EXISTS idx_browser_ingest_surface_freshness
    ON browser_ingest_events (platform, collection_surface, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_surface_freshness
    ON media_items (source, collection_surface, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_telegram_surface_freshness
    ON telegram_messages (collected_at DESC, collection_surface);
CREATE INDEX IF NOT EXISTS idx_whatsapp_surface_freshness
    ON whatsapp_messages (collected_at DESC, collection_surface);
