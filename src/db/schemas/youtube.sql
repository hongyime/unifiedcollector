-- YouTube V2 Schema

CREATE TABLE IF NOT EXISTS youtube_channels (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_channel_id VARCHAR(255) UNIQUE NOT NULL,
    title VARCHAR(500),
    description TEXT,
    custom_url VARCHAR(255),
    published_at TIMESTAMP,
    thumbnail_url TEXT,
    view_count BIGINT DEFAULT 0,
    subscriber_count BIGINT DEFAULT 0,
    video_count INTEGER DEFAULT 0,
    hidden_subscriber_count BOOLEAN DEFAULT FALSE,
    country VARCHAR(100),
    keywords TEXT[],
    profile_photo_media_id UUID,
    external_links JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_video_scan_at TIMESTAMPTZ,
    last_community_scan_at TIMESTAMPTZ,
    last_skip_reason TEXT,
    last_skip_at TIMESTAMPTZ,
    last_error TEXT,
    last_error_at TIMESTAMPTZ,
    discovered_from TEXT,
    collected_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_channel_youtube UNIQUE (platform_channel_id)
);

CREATE TABLE IF NOT EXISTS youtube_videos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_video_id VARCHAR(255) UNIQUE NOT NULL,
    channel_id UUID REFERENCES youtube_channels(id) ON DELETE SET NULL,
    title TEXT,
    description TEXT,
    tags TEXT[],
    category_id VARCHAR(50),
    duration VARCHAR(50), -- ISO 8601
    dimension VARCHAR(10), -- '2d', '3d'
    definition VARCHAR(10), -- 'hd', 'sd'
    caption VARCHAR(50), -- 'true' if available
    licensed_content BOOLEAN DEFAULT FALSE,
    view_count BIGINT DEFAULT 0,
    like_count INTEGER DEFAULT 0,
    dislike_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    favorite_count INTEGER DEFAULT 0,
    platform_published_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB,
    media_status TEXT NOT NULL DEFAULT 'pending',
    media_skip_reason TEXT,
    last_media_attempt_at TIMESTAMPTZ,
    transcript_status TEXT NOT NULL DEFAULT 'pending',
    transcript_error TEXT,
    last_transcript_attempt_at TIMESTAMPTZ,
    comments_status TEXT NOT NULL DEFAULT 'pending',
    comments_error TEXT,
    last_comments_attempt_at TIMESTAMPTZ,
    CONSTRAINT unique_platform_video_youtube UNIQUE (platform_video_id)
);

CREATE TABLE IF NOT EXISTS youtube_transcripts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    video_id UUID REFERENCES youtube_videos(id) ON DELETE CASCADE,
    language VARCHAR(10),
    is_generated BOOLEAN DEFAULT FALSE,
    content TEXT,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS youtube_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_comment_id VARCHAR(255) UNIQUE NOT NULL,
    video_id UUID REFERENCES youtube_videos(id) ON DELETE CASCADE,
    author_name VARCHAR(255),
    author_channel_id VARCHAR(255),
    author_thumbnail_url TEXT,
    text_original TEXT,
    like_count INTEGER DEFAULT 0,
    parent_comment_id VARCHAR(255),
    is_reply BOOLEAN DEFAULT FALSE,
    platform_published_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_comment_youtube UNIQUE (platform_comment_id)
);

CREATE TABLE IF NOT EXISTS youtube_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_channel_id VARCHAR(255) UNIQUE NOT NULL,
    source VARCHAR(50), -- 'related', 'search', 'manual'
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_spider_channel_youtube UNIQUE (platform_channel_id)
);

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

CREATE INDEX IF NOT EXISTS idx_yt_videos_channel ON youtube_videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_yt_comments_video ON youtube_comments(video_id);
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
CREATE INDEX IF NOT EXISTS idx_youtube_edges_source_channel ON youtube_edges(source_channel_id, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_youtube_edges_target_channel ON youtube_edges(target_channel_id, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_youtube_edges_target_handle ON youtube_edges(target_handle, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_youtube_edges_source_type_seen
    ON youtube_edges (source_channel_id, edge_type, last_seen DESC)
    WHERE source_channel_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_youtube_edges_record ON youtube_edges(source_table, source_record_id);
CREATE INDEX IF NOT EXISTS idx_youtube_profile_queue_status ON youtube_profile_queue(status, priority ASC, evidence_count DESC, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_youtube_profile_queue_pending_claim
    ON youtube_profile_queue (priority ASC, evidence_count DESC, last_seen DESC, profile_key)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_youtube_profile_queue_channel ON youtube_profile_queue(platform_channel_id);
CREATE INDEX IF NOT EXISTS idx_yt_comments_author_channel
    ON youtube_comments (author_channel_id)
    WHERE author_channel_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_youtube_channels_custom_url_lower
    ON youtube_channels (lower(custom_url))
    WHERE custom_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_youtube_spider_queue_pending_claim
    ON youtube_spider_queue (priority ASC, collected_at, id)
    WHERE status = 'pending';
