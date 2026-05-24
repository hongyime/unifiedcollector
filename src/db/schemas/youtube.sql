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

CREATE INDEX IF NOT EXISTS idx_yt_videos_channel ON youtube_videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_yt_comments_video ON youtube_comments(video_id);
