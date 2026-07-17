-- 2026-07-17: Facebook profile table populated best-effort by the browser
-- extension. Keep this late in lexical migration order because it backfills
-- from facebook_posts, which is created by add_media_kind_and_threads_fb_posts.

SET lock_timeout = '2s';

CREATE TABLE IF NOT EXISTS facebook_profiles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform_user_id text NOT NULL,
  username text NOT NULL,
  display_name text,
  bio text,
  followers_count integer,
  following_count integer,
  friends_count integer,
  is_person boolean,
  profile_pic_url text,
  external_url text,
  collected_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  metadata jsonb DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_facebook_profiles_platform_user_id
  ON facebook_profiles (platform_user_id);

CREATE INDEX IF NOT EXISTS idx_facebook_profiles_username_lower
  ON facebook_profiles (lower(username));

INSERT INTO facebook_profiles (platform_user_id, username, metadata)
SELECT DISTINCT trim(author_username), trim(author_username),
       jsonb_build_object('source', 'facebook_posts_author_backfill')
FROM facebook_posts
WHERE author_username IS NOT NULL
  AND trim(author_username) <> ''
ON CONFLICT (platform_user_id) DO UPDATE SET
  username = COALESCE(NULLIF(facebook_profiles.username, ''), EXCLUDED.username),
  metadata = facebook_profiles.metadata || EXCLUDED.metadata,
  updated_at = now();
