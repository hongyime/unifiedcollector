ALTER TABLE pinterest_profiles
    ADD COLUMN IF NOT EXISTS following_count INTEGER,
    ADD COLUMN IF NOT EXISTS monthly_views BIGINT;

ALTER TABLE pinterest_boards
    ADD COLUMN IF NOT EXISTS description TEXT,
    ADD COLUMN IF NOT EXISTS follower_count INTEGER;

ALTER TABLE pinterest_pins
    ADD COLUMN IF NOT EXISTS repin_count INTEGER,
    ADD COLUMN IF NOT EXISTS comment_count INTEGER;

ALTER TABLE airbnb_hosts
    ADD COLUMN IF NOT EXISTS bio TEXT,
    ADD COLUMN IF NOT EXISTS response_rate NUMERIC(5, 2);

ALTER TABLE airbnb_listings
    ADD COLUMN IF NOT EXISTS description TEXT,
    ADD COLUMN IF NOT EXISTS photos JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS price_amount NUMERIC(12, 2),
    ADD COLUMN IF NOT EXISTS price_currency TEXT,
    ADD COLUMN IF NOT EXISTS price_check_in DATE,
    ADD COLUMN IF NOT EXISTS price_check_out DATE,
    ADD COLUMN IF NOT EXISTS amenities JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS availability JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS availability_checked_at TIMESTAMPTZ;
