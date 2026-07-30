-- 2026-07-31: X/Twitter profile target queue and relationship edges.
-- The browser extension asks this queue which profile/media tab to visit next.
-- x_edges stores mention/reply/quote/repost evidence separately from raw posts.

SET lock_timeout = '2s';

CREATE TABLE IF NOT EXISTS x_profile_targets (
  username text PRIMARY KEY,
  source text NOT NULL DEFAULT 'seen',
  priority integer NOT NULL DEFAULT 50,
  status text NOT NULL DEFAULT 'pending',
  next_visit_at timestamptz NOT NULL DEFAULT now(),
  last_attempt_at timestamptz,
  last_success_at timestamptz,
  attempts integer NOT NULL DEFAULT 0,
  last_error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_x_profile_targets_due
  ON x_profile_targets (status, next_visit_at, priority DESC);

CREATE INDEX IF NOT EXISTS idx_x_profile_targets_updated
  ON x_profile_targets (updated_at DESC);

CREATE TABLE IF NOT EXISTS x_edges (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_username text,
  target_username text NOT NULL,
  post_id text,
  edge_type text NOT NULL,
  strength integer NOT NULL DEFAULT 50,
  evidence_url text,
  first_seen timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_x_edges_natural
  ON x_edges (
    lower(coalesce(source_username, '')),
    lower(target_username),
    coalesce(post_id, ''),
    edge_type
  );

CREATE INDEX IF NOT EXISTS idx_x_edges_source
  ON x_edges (lower(source_username), edge_type, last_seen DESC);

CREATE INDEX IF NOT EXISTS idx_x_edges_target
  ON x_edges (lower(target_username), edge_type, last_seen DESC);

CREATE INDEX IF NOT EXISTS idx_x_edges_post
  ON x_edges (post_id);

-- Your following list is Tier 1 for X. Seed it into the profile/media-tab queue.
INSERT INTO x_profile_targets (username, source, priority, status, metadata)
SELECT DISTINCT lower(trim(target_username)), 'following', 95, 'pending',
       jsonb_build_object('seeded_from', 'follow_edges', 'owner_account', owner_account)
FROM follow_edges
WHERE platform = 'x'
  AND direction = 'following'
  AND target_username IS NOT NULL
  AND trim(target_username) <> ''
ON CONFLICT (username) DO UPDATE SET
  priority = GREATEST(x_profile_targets.priority, EXCLUDED.priority),
  source = CASE
    WHEN x_profile_targets.source = 'manual' THEN x_profile_targets.source
    ELSE EXCLUDED.source
  END,
  status = CASE
    WHEN x_profile_targets.status = 'unavailable' THEN 'pending'
    ELSE x_profile_targets.status
  END,
  next_visit_at = LEAST(x_profile_targets.next_visit_at, now()),
  metadata = x_profile_targets.metadata || EXCLUDED.metadata,
  updated_at = now();

-- Seen authors and @mentions become lower-priority profile targets.
INSERT INTO x_profile_targets (username, source, priority, status, metadata)
SELECT DISTINCT lower(trim(author_username)), 'seen_author', 75, 'pending',
       jsonb_build_object('seeded_from', 'x_posts_author')
FROM x_posts
WHERE author_username IS NOT NULL
  AND trim(author_username) <> ''
ON CONFLICT (username) DO UPDATE SET
  priority = GREATEST(x_profile_targets.priority, EXCLUDED.priority),
  metadata = x_profile_targets.metadata || EXCLUDED.metadata,
  updated_at = now();

INSERT INTO x_profile_targets (username, source, priority, status, metadata)
SELECT DISTINCT lower(trim(both '@' FROM mention)), 'mention', 70, 'pending',
       jsonb_build_object('seeded_from', 'x_posts_mentions')
FROM x_posts
CROSS JOIN LATERAL unnest(coalesce(mentions, ARRAY[]::text[])) AS mention
WHERE mention IS NOT NULL
  AND trim(both '@' FROM mention) <> ''
ON CONFLICT (username) DO UPDATE SET
  priority = GREATEST(x_profile_targets.priority, EXCLUDED.priority),
  metadata = x_profile_targets.metadata || EXCLUDED.metadata,
  updated_at = now();

-- Backfill mention edges from the structured mentions array.
INSERT INTO x_edges (source_username, target_username, post_id, edge_type, strength, evidence_url, metadata)
SELECT DISTINCT lower(trim(author_username)),
       lower(trim(both '@' FROM mention)),
       platform_post_id,
       'mention',
       70,
       coalesce(metadata->>'verify_url', metadata->>'url'),
       jsonb_build_object('source', 'x_posts_mentions_backfill')
FROM x_posts
CROSS JOIN LATERAL unnest(coalesce(mentions, ARRAY[]::text[])) AS mention
WHERE author_username IS NOT NULL
  AND trim(author_username) <> ''
  AND mention IS NOT NULL
  AND trim(both '@' FROM mention) <> ''
ON CONFLICT DO NOTHING;

-- Capture authors as weak "seen" evidence so analyzer/dashboard can distinguish
-- "we saw this account post" from stronger direct interactions.
INSERT INTO x_edges (source_username, target_username, post_id, edge_type, strength, evidence_url, metadata)
SELECT DISTINCT NULL,
       lower(trim(author_username)),
       platform_post_id,
       'seen_author',
       20,
       coalesce(metadata->>'verify_url', metadata->>'url'),
       jsonb_build_object('source', 'x_posts_author_backfill')
FROM x_posts
WHERE author_username IS NOT NULL
  AND trim(author_username) <> ''
ON CONFLICT DO NOTHING;
