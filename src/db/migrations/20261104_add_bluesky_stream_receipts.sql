CREATE TABLE IF NOT EXISTS bluesky_stream_receipts (
    source_name text PRIMARY KEY CHECK (source_name = 'bluesky'),
    state text NOT NULL CHECK (state IN ('failed', 'ready')),
    first_cursor bigint,
    reconnect_cursor bigint,
    failure_count integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz,
    last_error text,
    checked_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE bluesky_stream_receipts
    ADD COLUMN IF NOT EXISTS failure_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_error text;
