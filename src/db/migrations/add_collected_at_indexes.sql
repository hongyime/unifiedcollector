-- 2026-07-03 (P2 review §2/§5): the freshness watchdog now runs max(collected_at)
-- per headless source every 5 min. On the big append-heavy tables that was a full
-- seq scan — github_commits (5.7M rows) took ~36s PER TICK, needless CPU/IO churn
-- on the shared box. A btree on collected_at turns each max() into an instant
-- index scan. These also speed up the analyzer/dashboard "recent activity" queries.
--
-- Plain (non-CONCURRENT) CREATE INDEX because migrate.py wraps each migration in a
-- transaction (CONCURRENTLY is illegal there). Builds briefly hold a write lock;
-- these collectors idle on long cycles (github 900s / strava 600s / search 600s),
-- and it is a one-time cost. media_items already has idx_media_collected, so it is
-- not repeated here.
CREATE INDEX IF NOT EXISTS idx_github_commits_collected  ON github_commits(collected_at);
CREATE INDEX IF NOT EXISTS idx_search_results_collected  ON search_results(collected_at);
CREATE INDEX IF NOT EXISTS idx_strava_activities_collected ON strava_activities(collected_at);
CREATE INDEX IF NOT EXISTS idx_website_pages_collected   ON website_pages(collected_at);
CREATE INDEX IF NOT EXISTS idx_youtube_videos_collected  ON youtube_videos(collected_at);
