CREATE TABLE IF NOT EXISTS realtime_media_deliveries (
    id BIGSERIAL PRIMARY KEY,
    media_item_id UUID,
    source TEXT NOT NULL,
    content_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'stored_only',
    reason TEXT,
    file_size BIGINT,
    content_type TEXT,
    dedupe_key TEXT,
    telegram_result JSONB,
    target_name TEXT,
    queued_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, content_id)
);

CREATE INDEX IF NOT EXISTS idx_realtime_media_deliveries_status_updated
    ON realtime_media_deliveries (status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_realtime_media_deliveries_source_updated
    ON realtime_media_deliveries (source, updated_at DESC);
