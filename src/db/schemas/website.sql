CREATE TABLE IF NOT EXISTS website_targets (
    domain VARCHAR(500) PRIMARY KEY,
    start_url TEXT NOT NULL,
    max_depth INT DEFAULT 3,
    last_scraped TIMESTAMP,
    spider_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_web_targets_status ON website_targets(spider_status);
