CREATE INDEX IF NOT EXISTS idx_browser_media_candidates_seen
  ON browser_media_candidates (last_seen DESC)
  INCLUDE (platform, outcome, needs_revisit);
