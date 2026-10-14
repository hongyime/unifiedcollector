CREATE TABLE IF NOT EXISTS snapchat_spotlights (
    spotlight_id TEXT PRIMARY KEY,
    username TEXT,
    title TEXT,
    story_url TEXT NOT NULL,
    media_url TEXT,
    thumbnail_url TEXT,
    posted_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_snapchat_spotlights_user_time
    ON snapchat_spotlights (username, collected_at DESC);

CREATE TABLE IF NOT EXISTS snapchat_highlights (
    highlight_id TEXT PRIMARY KEY,
    username TEXT,
    title TEXT,
    story_url TEXT NOT NULL,
    media_url TEXT,
    thumbnail_url TEXT,
    posted_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_snapchat_highlights_user_time
    ON snapchat_highlights (username, collected_at DESC);
