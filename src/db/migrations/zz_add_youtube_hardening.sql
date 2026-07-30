-- YouTube collector hardening: target honesty, graph edges, profile queue,
-- and richer media/community completeness tracking. Idempotent.

ALTER TABLE youtube_channels
    ADD COLUMN IF NOT EXISTS profile_photo_media_id UUID,
    ADD COLUMN IF NOT EXISTS external_links JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS last_video_scan_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_community_scan_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_skip_reason TEXT,
    ADD COLUMN IF NOT EXISTS last_skip_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_error TEXT,
    ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS discovered_from TEXT;

ALTER TABLE youtube_videos
    ADD COLUMN IF NOT EXISTS media_status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS media_skip_reason TEXT,
    ADD COLUMN IF NOT EXISTS last_media_attempt_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS transcript_status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS transcript_error TEXT,
    ADD COLUMN IF NOT EXISTS last_transcript_attempt_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS comments_status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS comments_error TEXT,
    ADD COLUMN IF NOT EXISTS last_comments_attempt_at TIMESTAMPTZ;

ALTER TABLE youtube_community_posts
    ADD COLUMN IF NOT EXISTS media_item_id UUID,
    ADD COLUMN IF NOT EXISTS media_status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS media_error TEXT,
    ADD COLUMN IF NOT EXISTS last_media_attempt_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS raw JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS youtube_edges (
    id BIGSERIAL PRIMARY KEY,
    source_channel_id VARCHAR(255),
    target_channel_id VARCHAR(255),
    target_handle VARCHAR(255),
    source_video_id VARCHAR(255),
    source_comment_id VARCHAR(255),
    source_post_id VARCHAR(255),
    edge_type VARCHAR(64) NOT NULL,
    strength INTEGER NOT NULL DEFAULT 50,
    evidence_text TEXT,
    evidence_url TEXT,
    source_table VARCHAR(128),
    source_record_id TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_youtube_edges_unique_evidence
    ON youtube_edges (
        COALESCE(source_table, ''),
        source_record_id,
        edge_type,
        COALESCE(source_channel_id, ''),
        COALESCE(target_channel_id, ''),
        COALESCE(target_handle, ''),
        COALESCE(source_video_id, ''),
        COALESCE(source_comment_id, ''),
        COALESCE(source_post_id, '')
    );

CREATE INDEX IF NOT EXISTS idx_youtube_edges_source_channel
    ON youtube_edges (source_channel_id, last_seen DESC);

CREATE INDEX IF NOT EXISTS idx_youtube_edges_target_channel
    ON youtube_edges (target_channel_id, last_seen DESC);

CREATE INDEX IF NOT EXISTS idx_youtube_edges_target_handle
    ON youtube_edges (target_handle, last_seen DESC);

CREATE TABLE IF NOT EXISTS youtube_profile_queue (
    profile_key TEXT PRIMARY KEY,
    key_type VARCHAR(32) NOT NULL,
    platform_channel_id VARCHAR(255),
    handle VARCHAR(255),
    source VARCHAR(64) NOT NULL DEFAULT 'discovery',
    priority INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    evidence_count INTEGER NOT NULL DEFAULT 1,
    discovered_from TEXT,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE youtube_profile_queue
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_youtube_profile_queue_status
    ON youtube_profile_queue (status, priority ASC, evidence_count DESC, last_seen DESC);

CREATE INDEX IF NOT EXISTS idx_youtube_profile_queue_pending_claim
    ON youtube_profile_queue (priority ASC, evidence_count DESC, last_seen DESC, profile_key)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_youtube_profile_queue_channel
    ON youtube_profile_queue (platform_channel_id);

CREATE INDEX IF NOT EXISTS idx_youtube_edges_source_type_seen
    ON youtube_edges (source_channel_id, edge_type, last_seen DESC)
    WHERE source_channel_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_youtube_edges_record
    ON youtube_edges (source_table, source_record_id);

CREATE INDEX IF NOT EXISTS idx_yt_comments_author_channel
    ON youtube_comments (author_channel_id)
    WHERE author_channel_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_youtube_channels_custom_url_lower
    ON youtube_channels (lower(custom_url))
    WHERE custom_url IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_youtube_spider_queue_pending_claim
    ON youtube_spider_queue (priority ASC, collected_at, id)
    WHERE status = 'pending';
