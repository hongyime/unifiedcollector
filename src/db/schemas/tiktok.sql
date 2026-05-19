CREATE TABLE IF NOT EXISTS tiktok_profiles (
    user_id VARCHAR(100) PRIMARY KEY,
    username VARCHAR(255) NOT NULL,
    nickname VARCHAR(500),
    follower_count INT,
    following_count INT,
    video_count INT,
    last_scraped TIMESTAMP,
    spider_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tt_profiles_status ON tiktok_profiles(spider_status);
