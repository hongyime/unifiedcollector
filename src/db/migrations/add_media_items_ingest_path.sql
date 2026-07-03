-- 2026-07-03 (P2 review §3): provenance for media_items.
-- Three ingestion paths write media_items with differently-keyed content_ids
-- (see base_collector.insert_media_item comment) but nothing recorded WHICH path
-- produced a row. Without it you can't audit extension-vs-headless coverage or
-- tell when one path silently stopped. Add a nullable tag (NULL = legacy/unknown):
--   'headless'  server-side cookie collectors (instagram/tiktok/youtube/lemon8/
--               strava/github/search/website)
--   'extension' browser-extension bridge (ig_ingest, /social/ingest)
--   'messaging' realtime messaging collectors (telegram/whatsapp/beeper)
-- Idempotent: ADD COLUMN IF NOT EXISTS.
ALTER TABLE media_items
    ADD COLUMN IF NOT EXISTS ingest_path VARCHAR(16);

-- Partial index for coverage-audit queries ("recent rows per path per source").
CREATE INDEX IF NOT EXISTS idx_media_ingest_path
    ON media_items(source, ingest_path)
    WHERE ingest_path IS NOT NULL;
