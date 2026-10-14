CREATE TABLE IF NOT EXISTS airbnb_reviews (
    review_id    TEXT PRIMARY KEY,
    listing_id   TEXT NOT NULL,
    reviewer_id  TEXT,
    review_date  TIMESTAMPTZ,
    rating       NUMERIC(3, 2),
    comments     TEXT,
    language     TEXT,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_airbnb_reviews_listing_date
    ON airbnb_reviews (listing_id, review_date DESC);
