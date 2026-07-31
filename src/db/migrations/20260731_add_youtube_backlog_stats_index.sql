CREATE INDEX IF NOT EXISTS idx_youtube_videos_backlog_stats
  ON youtube_videos (platform_video_id)
  INCLUDE (duration, collected_at, last_media_attempt_at);
