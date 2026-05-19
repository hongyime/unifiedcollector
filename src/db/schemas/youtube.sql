CREATE TABLE IF NOT EXISTS youtube_channels (
    channel_id VARCHAR(100) PRIMARY KEY,
    channel_name VARCHAR(500) NOT NULL,
    subscriber_count INT,
    video_count INT,
    last_scraped TIMESTAMP,
    spider_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_yt_channels_status ON youtube_channels(spider_status);
