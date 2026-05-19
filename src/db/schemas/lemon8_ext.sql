-- Lemon8 extended schema: discovered users and tags from feed/tag scraping

CREATE TABLE IF NOT EXISTS lemon8_discovered (
    id SERIAL PRIMARY KEY,
    entity_type VARCHAR(20) NOT NULL,
    entity_id VARCHAR(100) NOT NULL,
    entity_name VARCHAR(255),
    source VARCHAR(50) NOT NULL DEFAULT 'feed',
    discovered_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_ld_type ON lemon8_discovered(entity_type);
