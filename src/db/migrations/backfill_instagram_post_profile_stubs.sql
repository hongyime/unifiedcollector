-- Complete the Instagram NULL profile_id repair by creating minimal profile
-- stubs for posts whose platform_post_id embeds an author uid but no
-- instagram_profiles row exists yet. The profile collector later enriches the
-- same unique platform_user_id row.
SET lock_timeout = '2s';

WITH missing_uids AS (
    SELECT DISTINCT split_part(p.platform_post_id, '_'::text, 2) AS uid
    FROM instagram_posts p
    LEFT JOIN instagram_profiles ip
      ON ip.platform_user_id = split_part(p.platform_post_id, '_'::text, 2)
    WHERE p.profile_id IS NULL
      AND split_part(p.platform_post_id, '_'::text, 2) ~ '^[0-9]+$'
      AND ip.id IS NULL
)
INSERT INTO instagram_profiles (platform_user_id)
SELECT uid FROM missing_uids
ON CONFLICT (platform_user_id) DO NOTHING;

WITH candidates AS (
    SELECT p.id AS post_id, ip.id AS profile_id
    FROM instagram_posts p
    JOIN instagram_profiles ip
      ON ip.platform_user_id = split_part(p.platform_post_id, '_'::text, 2)
    WHERE p.profile_id IS NULL
      AND split_part(p.platform_post_id, '_'::text, 2) ~ '^[0-9]+$'
)
UPDATE instagram_posts p
SET profile_id = candidates.profile_id
FROM candidates
WHERE p.id = candidates.post_id
  AND p.profile_id IS NULL;

RESET lock_timeout;
