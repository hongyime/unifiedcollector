CREATE TABLE IF NOT EXISTS strava_athletes (
    athlete_id VARCHAR(100) PRIMARY KEY,
    username VARCHAR(255),
    full_name VARCHAR(500),
    follower_count INT,
    activity_count INT,
    last_scraped TIMESTAMP,
    spider_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sv_athletes_status ON strava_athletes(spider_status);
