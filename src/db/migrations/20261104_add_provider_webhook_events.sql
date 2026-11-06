CREATE TABLE IF NOT EXISTS provider_webhook_events (
    provider TEXT NOT NULL CHECK (provider IN ('strava', 'paypal')),
    delivery_key TEXT NOT NULL,
    account_identity TEXT NOT NULL,
    event_type TEXT NOT NULL,
    resource_identity TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted' CHECK (status IN ('accepted', 'processing', 'hydrated', 'ignored', 'failed')),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    PRIMARY KEY (provider, delivery_key)
);

CREATE INDEX IF NOT EXISTS idx_provider_webhook_events_pending
    ON provider_webhook_events (provider, status, received_at)
    WHERE status = 'accepted';
