-- DATA-002: source_url coverage on new media writes.
--
-- Investigation: the 227 rows with NULL source_url are legitimate legacy
-- state. All are ``content_type='profile_photo'`` (Instagram profile_<id>,
-- YouTube channel avatar, etc.) which per the README source_url contract
-- have no stable public URL for the photo itself. Profile photos are keyed
-- by (source, entity_id), not by a shareable page URL.
--
-- This migration:
--   1. Adds a NOT VALID CHECK constraint so FUTURE inserts for non-profile
--      content types must carry source_url. Existing NULL rows are grandfathered
--      by the NOT VALID clause; we do not attempt to VALIDATE.
--   2. Adds a partial index on the (source, content_type) filter that the
--      dashboard's source_url-coverage health signal should use going forward,
--      so the query "count of non-profile media without source_url" is cheap.
--
-- The constraint is deliberately soft — no VALIDATE — because inserting a
-- correctly-shaped profile_photo row without source_url must remain legal.
-- Application-level enforcement happens in insert_media_item, where each
-- collector's _build_<source>_source_url() is called before writing.

ALTER TABLE media_items
    DROP CONSTRAINT IF EXISTS media_items_source_url_required_for_non_profile;

ALTER TABLE media_items
    ADD CONSTRAINT media_items_source_url_required_for_non_profile
    CHECK (
        source_url IS NOT NULL
        OR content_type IN ('profile_photo', 'avatar', 'thumbnail')
        OR source IN ('whatsapp', 'beeper')  -- realtime sources use stable URI schemes/NULL
    )
    NOT VALID;

-- Partial index for cheap dashboard coverage checks:
--   SELECT count(*) FROM media_items
--   WHERE source_url IS NULL AND content_type NOT IN ('profile_photo','avatar','thumbnail')
--     AND source NOT IN ('whatsapp','beeper');
CREATE INDEX IF NOT EXISTS idx_media_items_missing_source_url
    ON media_items (source, content_type)
    WHERE source_url IS NULL
      AND content_type NOT IN ('profile_photo', 'avatar', 'thumbnail')
      AND source NOT IN ('whatsapp', 'beeper');
