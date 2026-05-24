-- TikTok V2 Schema

CREATE TABLE IF NOT EXISTS tiktok_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(255),
    nickname VARCHAR(255),
    avatar_url TEXT,
    bio TEXT,
    following_count INTEGER DEFAULT 0,
    followers_count INTEGER DEFAULT 0,
    heart_count INTEGER DEFAULT 0,
    video_count INTEGER DEFAULT 0,
    digg_count INTEGER DEFAULT 0,
    is_verified BOOLEAN DEFAULT FALSE,
    is_private BOOLEAN DEFAULT FALSE,
    collected_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_user_tiktok UNIQUE (platform_user_id)
);

CREATE TABLE IF NOT EXISTS tiktok_posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_post_id VARCHAR(255) UNIQUE NOT NULL,
    profile_id UUID REFERENCES tiktok_profiles(id) ON DELETE SET NULL,
    video_url TEXT,
    cover_image_url TEXT,
    title TEXT, -- Video caption
    description TEXT,
    hashtags TEXT[],
    mentions TEXT[],
    challenges TEXT[], -- TikTok challenges
    music_id VARCHAR(255),
    music_title TEXT,
    music_author TEXT,
    music_duration INTEGER,
    effect_ids TEXT[],
    stickers TEXT[],
    duet_enabled BOOLEAN DEFAULT FALSE,
    stitch_enabled BOOLEAN DEFAULT FALSE,
    view_count INTEGER DEFAULT 0,
    like_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    share_count INTEGER DEFAULT 0,
    duration INTEGER,
    create_time TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB,
    CONSTRAINT unique_platform_post_tiktok UNIQUE (platform_post_id)
);

CREATE TABLE IF NOT EXISTS tiktok_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_comment_id VARCHAR(255) UNIQUE NOT NULL,
    post_id UUID REFERENCES tiktok_posts(id) ON DELETE CASCADE,
    author_username VARCHAR(255),
    author_nickname VARCHAR(255),
    author_avatar_url TEXT,
    text TEXT,
    like_count INTEGER DEFAULT 0,
    reply_count INTEGER DEFAULT 0,
    parent_comment_id VARCHAR(255),
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_comment_tiktok UNIQUE (platform_comment_id)
);

CREATE TABLE IF NOT EXISTS tiktok_download_tracker (
    username VARCHAR(100),
    video_id VARCHAR(100),
    file_path TEXT,
    download_time TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (username, video_id)
);

CREATE TABLE IF NOT EXISTS tiktok_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(255),
    source VARCHAR(50), -- 'followers', 'following', 'video_likers', 'manual'
    source_post_id VARCHAR(255),
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_spider_user_tiktok UNIQUE (platform_user_id)
);

CREATE INDEX IF NOT EXISTS idx_tt_posts_profile ON tiktok_posts(profile_id);
CREATE INDEX IF NOT EXISTS idx_tt_comments_post ON tiktok_comments(post_id);
