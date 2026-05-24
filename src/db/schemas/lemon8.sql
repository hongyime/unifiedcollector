-- Lemon8 V2 Schema

CREATE TABLE IF NOT EXISTS lemon8_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(255),
    nickname VARCHAR(255),
    avatar_url TEXT,
    bio TEXT,
    following_count INTEGER DEFAULT 0,
    followers_count INTEGER DEFAULT 0,
    like_count INTEGER DEFAULT 0,
    collected_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_user_lemon8 UNIQUE (platform_user_id)
);

CREATE TABLE IF NOT EXISTS lemon8_posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_post_id VARCHAR(255) UNIQUE NOT NULL,
    profile_id UUID REFERENCES lemon8_profiles(id) ON DELETE SET NULL,
    title TEXT,
    description TEXT,
    image_urls TEXT[],
    video_url TEXT,
    music_title TEXT,
    hashtags TEXT[],
    mention_usernames TEXT[],
    location_name TEXT,
    like_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    share_count INTEGER DEFAULT 0,
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB,
    CONSTRAINT unique_platform_post_lemon8 UNIQUE (platform_post_id)
);

CREATE TABLE IF NOT EXISTS lemon8_discovered (
    entity_type VARCHAR(20), -- 'user', 'tag'
    entity_id VARCHAR(255) PRIMARY KEY,
    entity_name VARCHAR(255),
    source VARCHAR(50),
    found_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS lemon8_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id VARCHAR(255) UNIQUE NOT NULL,
    source VARCHAR(50), -- 'followers', 'following', 'manual'
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_spider_user_lemon8 UNIQUE (platform_user_id)
);

CREATE INDEX IF NOT EXISTS idx_lemon8_posts_profile ON lemon8_posts(profile_id);
