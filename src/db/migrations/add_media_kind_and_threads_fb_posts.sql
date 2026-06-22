-- 2026-06-22: media_items.kind (post|story|highlight) + dedicated posts tables
-- for Threads and Facebook (extension scrapers). Idempotent.

ALTER TABLE media_items ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'post';

CREATE TABLE IF NOT EXISTS threads_posts (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform_post_id   varchar NOT NULL UNIQUE,
  author_username    varchar,
  caption            text,
  hashtags           text[],
  mentions           text[],
  likes_count        integer,
  comments_count     integer,   -- replies
  reposts_count      integer,
  media_type         varchar,
  platform_created_at timestamptz,
  collected_at       timestamptz DEFAULT now(),
  metadata           jsonb DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS facebook_posts (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform_post_id   varchar NOT NULL UNIQUE,
  author_username    varchar,
  caption            text,
  hashtags           text[],
  mentions           text[],
  likes_count        integer,
  comments_count     integer,
  shares_count       integer,
  media_type         varchar,
  platform_created_at timestamptz,
  collected_at       timestamptz DEFAULT now(),
  metadata           jsonb DEFAULT '{}'::jsonb
);
