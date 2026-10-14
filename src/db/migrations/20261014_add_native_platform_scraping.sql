CREATE TABLE IF NOT EXISTS browser_native_scrape_queue (
    id BIGSERIAL PRIMARY KEY,
    platform TEXT NOT NULL CHECK (platform IN ('pinterest', 'snapchat', 'airbnb')),
    target_url TEXT NOT NULL,
    username TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'completed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    next_visit_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claim_expires_at TIMESTAMPTZ,
    last_error TEXT,
    last_result_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, target_url)
);

CREATE INDEX IF NOT EXISTS idx_browser_native_scrape_due
    ON browser_native_scrape_queue (platform, status, next_visit_at, priority DESC, id);

CREATE TABLE IF NOT EXISTS pinterest_boards (
    board_id TEXT PRIMARY KEY,
    username TEXT,
    name TEXT,
    board_url TEXT NOT NULL,
    pin_count INTEGER,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pinterest_boards_user
    ON pinterest_boards (username, collected_at DESC);

CREATE TABLE IF NOT EXISTS pinterest_pins (
    pin_id TEXT PRIMARY KEY,
    username TEXT,
    board_id TEXT,
    board_url TEXT,
    title TEXT,
    description TEXT,
    image_url TEXT,
    destination_url TEXT,
    pin_url TEXT NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pinterest_pins_user_time
    ON pinterest_pins (username, collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_pinterest_pins_board_time
    ON pinterest_pins (board_id, collected_at DESC);

CREATE TABLE IF NOT EXISTS snapchat_stories (
    story_id TEXT PRIMARY KEY,
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

CREATE INDEX IF NOT EXISTS idx_snapchat_stories_user_time
    ON snapchat_stories (username, collected_at DESC);

CREATE TABLE IF NOT EXISTS airbnb_hosts (
    host_id TEXT PRIMARY KEY,
    display_name TEXT,
    profile_url TEXT NOT NULL,
    avatar_url TEXT,
    rating NUMERIC(3, 2),
    reviews_count INTEGER,
    host_since TEXT,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS airbnb_listings (
    listing_id TEXT PRIMARY KEY,
    host_id TEXT,
    host_name TEXT,
    host_profile_url TEXT,
    title TEXT,
    listing_url TEXT NOT NULL,
    location TEXT,
    image_url TEXT,
    rating NUMERIC(3, 2),
    reviews_count INTEGER,
    property_type TEXT,
    bedrooms NUMERIC(4, 1),
    bathrooms NUMERIC(4, 1),
    max_guests INTEGER,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_airbnb_listings_host_time
    ON airbnb_listings (host_id, collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_airbnb_listings_time
    ON airbnb_listings (collected_at DESC);
