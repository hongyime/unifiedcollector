-- 2026-07-17: X/Twitter profile table populated by the browser extension.
-- Idempotent and additive. The identity key intentionally remains the handle
-- value already used by x_posts.author_username, so existing analyzer links do
-- not change when this normalized profile table appears.

SET lock_timeout = '2s';

CREATE TABLE IF NOT EXISTS x_profiles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform_user_id text NOT NULL,
  username text NOT NULL,
  display_name text,
  bio text,
  followers_count integer,
  following_count integer,
  posts_count integer,
  is_verified boolean,
  is_private boolean,
  profile_pic_url text,
  external_url text,
  location text,
  joined_text text,
  collected_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  metadata jsonb DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_x_profiles_platform_user_id
  ON x_profiles (platform_user_id);

CREATE INDEX IF NOT EXISTS idx_x_profiles_username_lower
  ON x_profiles (lower(username));

INSERT INTO x_profiles (platform_user_id, username, metadata)
SELECT DISTINCT trim(author_username), trim(author_username),
       jsonb_build_object('source', 'x_posts_author_backfill')
FROM x_posts
WHERE author_username IS NOT NULL
  AND trim(author_username) <> ''
ON CONFLICT (platform_user_id) DO UPDATE SET
  username = COALESCE(NULLIF(x_profiles.username, ''), EXCLUDED.username),
  metadata = x_profiles.metadata || EXCLUDED.metadata,
  updated_at = now();
