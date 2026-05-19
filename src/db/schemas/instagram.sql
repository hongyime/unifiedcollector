CREATE TABLE IF NOT EXISTS instagram_profiles (
    user_id VARCHAR(100) PRIMARY KEY,
    username VARCHAR(255) NOT NULL,
    full_name VARCHAR(500),
    is_private BOOLEAN,
    followers_count INT,
    following_count INT,
    post_count INT,
    bio TEXT,
    last_scraped TIMESTAMP,
    spider_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_ig_profiles_status ON instagram_profiles(spider_status);
