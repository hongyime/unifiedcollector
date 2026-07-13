-- 2026-07-13: partial index for the Stories dashboard view.
-- Ephemeral media (Instagram stories/highlights, and future whatsapp status /
-- telegram stories / tiktok stories) lives under media_items.kind. The Stories
-- page groups + browses by kind IN ('story','highlight'), which was a full seq
-- scan over the ~500k-row media_items table (~170s, tripping the endpoint
-- timeout). Only ~3.8k rows match those kinds, so a PARTIAL index on them is
-- tiny and makes the overview + browse-by-kind queries fast. Ordered by
-- collected_at DESC to serve the newest-first ORDER BY directly.
CREATE INDEX IF NOT EXISTS idx_media_kind_ephemeral
    ON media_items (kind, collected_at DESC)
    WHERE kind IN ('story', 'highlight');
