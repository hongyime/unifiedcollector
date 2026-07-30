-- Backfill YouTube completeness status columns from historical rows.
-- Kept separate because zz_add_youtube_hardening.sql may already be applied.

UPDATE youtube_videos v
SET media_status = 'stored',
    media_skip_reason = NULL,
    last_media_attempt_at = COALESCE(last_media_attempt_at, NOW())
WHERE EXISTS (
    SELECT 1
    FROM media_items mi
    WHERE mi.source = 'youtube'
      AND mi.content_id = 'video_' || v.platform_video_id
)
  AND v.media_status IS DISTINCT FROM 'stored';

UPDATE youtube_videos v
SET transcript_status = 'stored',
    transcript_error = NULL,
    last_transcript_attempt_at = COALESCE(last_transcript_attempt_at, NOW())
WHERE EXISTS (
    SELECT 1
    FROM youtube_transcripts t
    WHERE t.video_id = v.id
)
  AND v.transcript_status IS DISTINCT FROM 'stored';

UPDATE youtube_videos v
SET comments_status = 'stored',
    comments_error = NULL,
    last_comments_attempt_at = COALESCE(last_comments_attempt_at, NOW())
WHERE EXISTS (
    SELECT 1
    FROM youtube_comments c
    WHERE c.video_id = v.id
)
  AND v.comments_status IS DISTINCT FROM 'stored';

UPDATE youtube_community_posts cp
SET media_status = 'stored',
    media_error = NULL,
    last_media_attempt_at = COALESCE(last_media_attempt_at, NOW())
WHERE (
    cp.media_item_id IS NOT NULL
    OR EXISTS (
        SELECT 1
        FROM media_items mi
        WHERE mi.source = 'youtube'
          AND mi.content_id = 'community_' || cp.platform_post_id
    )
)
  AND cp.media_status IS DISTINCT FROM 'stored';
