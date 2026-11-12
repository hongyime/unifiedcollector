CREATE TABLE IF NOT EXISTS collector_feed_cursors (
    source TEXT NOT NULL,
    surface TEXT NOT NULL,
    cursor TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, surface)
);
