-- Instagram V2 Schema
-- Combined profiles, posts, comments, and spidering

CREATE TABLE IF NOT EXISTS instagram_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(255),
    full_name TEXT,
    bio TEXT,
    followers_count INTEGER DEFAULT 0,
    following_count INTEGER DEFAULT 0,
    posts_count INTEGER DEFAULT 0,
    is_verified BOOLEAN DEFAULT FALSE,
    is_private BOOLEAN DEFAULT FALSE,
    profile_pic_url TEXT,
    email TEXT, -- vestigial: instaloader never populates this field (always NULL)
    phone TEXT, -- vestigial: instaloader never populates this field (always NULL)
    external_url TEXT,
    collected_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_user_instagram UNIQUE (platform_user_id)
);

CREATE TABLE IF NOT EXISTS instagram_posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_post_id VARCHAR(255) UNIQUE NOT NULL,
    profile_id UUID REFERENCES instagram_profiles(id) ON DELETE SET NULL,
    media_type VARCHAR(50), -- 'image', 'video', 'carousel', 'reel', 'story'
    caption TEXT,
    hashtags TEXT[],
    mentions TEXT[],
    location_name TEXT,       -- populated when INSTA_DOWNLOAD_GEOTAGS=true (default)
    location_lat FLOAT,       -- from node.location.lat; requires geotags enabled
    location_lng FLOAT,       -- from node.location.lng; requires geotags enabled
    likes_count INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0,
    shares_count INTEGER DEFAULT 0,
    saves_count INTEGER DEFAULT 0,
    reach_count INTEGER,
    impressions_count INTEGER,
    video_duration INTEGER,
    music_title TEXT,
    music_author TEXT,
    is_ad BOOLEAN DEFAULT FALSE,
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB, -- Raw API response for flexibility
    CONSTRAINT unique_platform_post_instagram UNIQUE (platform_post_id)
);

CREATE TABLE IF NOT EXISTS instagram_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_comment_id VARCHAR(255) UNIQUE NOT NULL,
    post_id UUID REFERENCES instagram_posts(id) ON DELETE CASCADE,
    author_username VARCHAR(255),
    author_platform_id VARCHAR(255),
    author_followers_count INTEGER,
    text TEXT,
    like_count INTEGER DEFAULT 0,
    parent_comment_id VARCHAR(255), -- For replies
    is_reply BOOLEAN DEFAULT FALSE,
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_comment_instagram UNIQUE (platform_comment_id)
);

CREATE TABLE IF NOT EXISTS instagram_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(255),
    source VARCHAR(50), -- 'followers', 'following', 'manual', 'likers'
    source_post_id VARCHAR(255),
    priority INTEGER DEFAULT 5, -- 1-10, lower = higher priority
    status VARCHAR(20) DEFAULT 'pending', -- pending, processing, completed, failed
    attempts INTEGER DEFAULT 0,
    last_attempt TIMESTAMP,
    error_message TEXT,
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_spider_user_instagram UNIQUE (platform_user_id)
);

-- Indices for performance
CREATE INDEX IF NOT EXISTS idx_ig_profiles_username ON instagram_profiles(username);
CREATE INDEX IF NOT EXISTS idx_ig_posts_profile ON instagram_posts(profile_id);
CREATE INDEX IF NOT EXISTS idx_ig_comments_post ON instagram_comments(post_id);
CREATE INDEX IF NOT EXISTS idx_ig_spider_status ON instagram_spider_queue(status, priority);
