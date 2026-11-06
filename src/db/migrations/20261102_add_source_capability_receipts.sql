CREATE TABLE IF NOT EXISTS source_capability_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL,
    account_ref TEXT,
    capability TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('proven', 'failed', 'rolled_back')),
    proof JSONB NOT NULL,
    rollback JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_source_capability_receipts_latest
    ON source_capability_receipts (source, capability, created_at DESC);
