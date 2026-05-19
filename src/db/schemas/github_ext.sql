-- GitHub extended schema: profile photo history, social graph, avatar batch tracking

CREATE TABLE IF NOT EXISTS profile_photo_history (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(100) NOT NULL,
    username VARCHAR(255),
    avatar_url TEXT,
    avatar_md5 VARCHAR(32),
    avatar_phash VARCHAR(64),
    avatar_blob BYTEA,
    file_path TEXT,
    detected_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pph_user ON profile_photo_history(user_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_pph_md5 ON profile_photo_history(avatar_md5);

CREATE TABLE IF NOT EXISTS graph_edges (
    id SERIAL PRIMARY KEY,
    source_user VARCHAR(100) NOT NULL,
    target_user VARCHAR(100) NOT NULL,
    edge_type VARCHAR(30) NOT NULL DEFAULT 'follows',
    discovered_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(source_user, target_user, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_ge_source ON graph_edges(source_user);
CREATE INDEX IF NOT EXISTS idx_ge_target ON graph_edges(target_user);
CREATE INDEX IF NOT EXISTS idx_ge_type ON graph_edges(edge_type);

CREATE TABLE IF NOT EXISTS avatar_downloads (
    user_id BIGINT PRIMARY KEY,
    username VARCHAR(255),
    md5_hash VARCHAR(32),
    file_path TEXT,
    file_size BIGINT,
    downloaded_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ad_md5 ON avatar_downloads(md5_hash);
