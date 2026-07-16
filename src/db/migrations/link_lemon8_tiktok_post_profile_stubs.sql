-- Link Lemon8/TikTok posts to profile stubs created by the previous backfill.
-- Kept separate because data-modifying CTE inserts are not visible to sibling
-- subqueries in the same statement snapshot.

SET lock_timeout = '2s';

WITH lemon8_profile_matches AS (
    SELECT p.id AS post_id, profile.id AS profile_id
    FROM lemon8_posts p
    JOIN LATERAL (
        SELECT lp.id
        FROM lemon8_profiles lp
        WHERE LOWER(lp.username) = LOWER(BTRIM(p.username))
           OR LOWER(lp.platform_user_id) = LOWER(BTRIM(p.username))
        ORDER BY (lp.platform_user_id ~ '^(user[0-9]+|[0-9]{6,})$') DESC,
                 lp.updated_at DESC NULLS LAST
        LIMIT 1
    ) profile ON TRUE
    WHERE p.profile_id IS NULL
      AND NULLIF(BTRIM(p.username), '') IS NOT NULL
)
UPDATE lemon8_posts p
SET profile_id = m.profile_id
FROM lemon8_profile_matches m
WHERE p.id = m.post_id
  AND p.profile_id IS NULL;

WITH tiktok_post_keys AS (
    SELECT
        p.id AS post_id,
        LOWER(BTRIM(p.metadata->>'user')) AS handle,
        COALESCE(
            NULLIF(BTRIM(p.metadata->>'secUid'), ''),
            'handle:' || LOWER(BTRIM(p.metadata->>'user'))
        ) AS profile_key
    FROM tiktok_posts p
    WHERE p.profile_id IS NULL
      AND NULLIF(BTRIM(p.metadata->>'user'), '') IS NOT NULL
),
tiktok_profile_matches AS (
    SELECT k.post_id, profile.id AS profile_id
    FROM tiktok_post_keys k
    JOIN LATERAL (
        SELECT tp.id
        FROM tiktok_profiles tp
        WHERE tp.platform_user_id = k.profile_key
           OR LOWER(tp.username) = k.handle
        ORDER BY (tp.platform_user_id = k.profile_key) DESC,
                 tp.updated_at DESC NULLS LAST
        LIMIT 1
    ) profile ON TRUE
)
UPDATE tiktok_posts p
SET profile_id = m.profile_id
FROM tiktok_profile_matches m
WHERE p.id = m.post_id
  AND p.profile_id IS NULL;

RESET lock_timeout;
