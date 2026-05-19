CREATE TABLE IF NOT EXISTS lemon8_profiles (
    user_id VARCHAR(100) PRIMARY KEY,
    username VARCHAR(255) NOT NULL,
    display_name VARCHAR(500),
    follower_count INT,
    post_count INT,
    last_scraped TIMESTAMP,
    spider_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_l8_profiles_status ON lemon8_profiles(spider_status);
