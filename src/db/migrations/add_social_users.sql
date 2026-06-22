-- 2026-06-22: universal user registry — every user/id encountered on any platform
-- (follows graph, comment authors, tagged users, post authors, likers/reactors the
-- page loaded). Idempotent.
CREATE TABLE IF NOT EXISTS social_users (
  platform          text NOT NULL,
  uid               text NOT NULL,          -- platform_user_id if known, else username
  platform_user_id  text,
  username          text,
  display_name      text,
  first_seen        timestamptz DEFAULT now(),
  last_seen         timestamptz DEFAULT now(),
  times_seen        int DEFAULT 1,
  contexts          text[] DEFAULT '{}',    -- follow, comment, tagged, author, like, reaction, seen
  metadata          jsonb DEFAULT '{}'::jsonb,
  PRIMARY KEY (platform, uid)
);
CREATE INDEX IF NOT EXISTS idx_social_users_username ON social_users(platform, username);
