-- 2026-06-22: X (Twitter) post engagement + YouTube community posts. Idempotent.
CREATE TABLE IF NOT EXISTS x_posts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform_post_id varchar NOT NULL UNIQUE,
  author_username varchar,
  caption text, hashtags text[], mentions text[],
  likes_count int, comments_count int, reposts_count int, quote_count int, views_count int,
  media_type varchar, platform_created_at timestamptz,
  collected_at timestamptz DEFAULT now(), metadata jsonb DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS youtube_community_posts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform_post_id varchar NOT NULL UNIQUE,
  channel_id varchar, text text,
  likes_count int, comments_count int,
  has_image boolean, image_url text,
  platform_published_at timestamptz, collected_at timestamptz DEFAULT now(),
  metadata jsonb DEFAULT '{}'::jsonb
);
