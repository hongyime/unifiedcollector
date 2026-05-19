CREATE TABLE IF NOT EXISTS search_queries (
    id SERIAL PRIMARY KEY,
    query_text TEXT NOT NULL,
    engine VARCHAR(50) NOT NULL DEFAULT 'google',
    schedule VARCHAR(100),
    last_run TIMESTAMP,
    spider_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    metadata JSONB DEFAULT '{}',
    UNIQUE (query_text, engine)
);

CREATE INDEX IF NOT EXISTS idx_sq_status ON search_queries(spider_status);
