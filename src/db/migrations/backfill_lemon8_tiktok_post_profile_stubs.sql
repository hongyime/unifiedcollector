-- Backfill recoverable Lemon8/TikTok post profile_id values from owner handles.
-- Idempotent and intentionally additive: creates minimal profile stubs only
-- when no profile already matches the post owner handle.

SET lock_timeout = '2s';

WITH lemon8_handles AS (
    SELECT DISTINCT LOWER(BTRIM(username)) AS handle
    FROM lemon8_posts
    WHERE profile_id IS NULL
      AND NULLIF(BTRIM(username), '') IS NOT NULL
),
inserted_lemon8_profiles AS (
    INSERT INTO lemon8_profiles (platform_user_id, username, updated_at)
    SELECT h.handle, h.handle, NOW()
    FROM lemon8_handles h
    WHERE NOT EXISTS (
        SELECT 1
        FROM lemon8_profiles p
        WHERE LOWER(p.username) = h.handle
           OR LOWER(p.platform_user_id) = h.handle
    )
    ON CONFLICT (platform_user_id) DO NOTHING
    RETURNING id
),
lemon8_profile_matches AS (
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

WITH tiktok_author_source AS (
    SELECT
        LOWER(BTRIM(metadata->>'user')) AS handle,
        COALESCE(
            NULLIF(BTRIM(metadata->>'secUid'), ''),
            'handle:' || LOWER(BTRIM(metadata->>'user'))
        ) AS profile_key,
        metadata
    FROM tiktok_posts
    WHERE profile_id IS NULL
      AND NULLIF(BTRIM(metadata->>'user'), '') IS NOT NULL
),
tiktok_authors AS (
    SELECT DISTINCT ON (profile_key)
        handle,
        profile_key,
        metadata
    FROM tiktok_author_source
    ORDER BY profile_key
),
inserted_tiktok_profiles AS (
    INSERT INTO tiktok_profiles (
        platform_user_id, username, nickname, avatar_url, bio,
        is_verified, is_private, updated_at
    )
    SELECT
        a.profile_key,
        a.handle,
        NULLIF(a.metadata->>'nickname', ''),
        COALESCE(
            NULLIF(a.metadata->>'avatarLarger', ''),
            NULLIF(a.metadata->>'avatarMedium', ''),
            NULLIF(a.metadata->>'avatarThumb', '')
        ),
        NULLIF(a.metadata->>'signature', ''),
        LOWER(COALESCE(a.metadata->>'verified', 'false')) IN ('1', 'true', 't', 'yes', 'y'),
        LOWER(COALESCE(a.metadata->>'privateAccount', 'false')) IN ('1', 'true', 't', 'yes', 'y'),
        NOW()
    FROM tiktok_authors a
    WHERE NOT EXISTS (
        SELECT 1
        FROM tiktok_profiles p
        WHERE p.platform_user_id = a.profile_key
           OR LOWER(p.username) = a.handle
    )
    ON CONFLICT (platform_user_id) DO UPDATE SET
        username = COALESCE(tiktok_profiles.username, EXCLUDED.username),
        nickname = COALESCE(tiktok_profiles.nickname, EXCLUDED.nickname),
        avatar_url = COALESCE(tiktok_profiles.avatar_url, EXCLUDED.avatar_url),
        bio = COALESCE(tiktok_profiles.bio, EXCLUDED.bio),
        is_verified = tiktok_profiles.is_verified OR EXCLUDED.is_verified,
        is_private = tiktok_profiles.is_private OR EXCLUDED.is_private,
        updated_at = NOW()
    RETURNING id
),
tiktok_post_keys AS (
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
