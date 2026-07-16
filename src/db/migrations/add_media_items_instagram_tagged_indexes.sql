-- Speed analyzer /entities/{id}/associates, which looks up Instagram tagged
-- media by tagged username and by owner uid parsed from content_id
-- ("tagged_<media_pk>_<owner_pk>_<idx>"). Keep this additive/idempotent so it
-- is safe on live collector databases.
SET lock_timeout = '2s';

CREATE INDEX IF NOT EXISTS idx_media_ig_tagged_entity_owner_media
ON media_items (
    entity_name,
    (split_part(content_id::text, '_'::text, 3)),
    (split_part(content_id::text, '_'::text, 2))
)
WHERE source = 'instagram'
  AND kind = 'tagged'
  AND entity_name IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_media_ig_tagged_owner_entity_media
ON media_items (
    (split_part(content_id::text, '_'::text, 3)),
    entity_name,
    (split_part(content_id::text, '_'::text, 2))
)
WHERE source = 'instagram'
  AND kind = 'tagged'
  AND split_part(content_id::text, '_'::text, 3) <> '';

RESET lock_timeout;
