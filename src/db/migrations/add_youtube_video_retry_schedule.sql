-- Persist media retry backoff for YouTube video archives.
-- Older live databases predate youtube_videos.next_attempt_at, while the
-- collector already uses it to avoid retrying transient yt-dlp failures in a
-- tight loop.

ALTER TABLE youtube_videos
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_youtube_videos_next_attempt
    ON youtube_videos (next_attempt_at)
    WHERE next_attempt_at IS NOT NULL;
