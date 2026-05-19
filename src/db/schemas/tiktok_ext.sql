-- TikTok extended schema: download tracker for resume support

CREATE TABLE IF NOT EXISTS tiktok_download_tracker (
    id SERIAL PRIMARY KEY,
    username VARCHAR(255) NOT NULL,
    video_id VARCHAR(100) NOT NULL,
    file_path TEXT,
    file_size BIGINT,
    status VARCHAR(20) NOT NULL DEFAULT 'complete',
    downloaded_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(username, video_id)
);

CREATE INDEX IF NOT EXISTS idx_tdt_user ON tiktok_download_tracker(username);
