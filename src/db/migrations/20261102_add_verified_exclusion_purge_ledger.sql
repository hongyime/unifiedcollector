CREATE TABLE IF NOT EXISTS verified_exclusion_purge_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    manifest_id UUID NOT NULL REFERENCES verified_exclusion_manifests(id),
    status TEXT NOT NULL CHECK (status IN ('prepared', 'committed', 'failed', 'rolled_back')),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS verified_exclusion_media_cleanup (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_id UUID NOT NULL REFERENCES verified_exclusion_purge_receipts(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'deleted', 'shared', 'missing', 'failed')),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (receipt_id, file_path)
);

CREATE INDEX IF NOT EXISTS idx_verified_exclusion_media_cleanup_pending
    ON verified_exclusion_media_cleanup (status, created_at)
    WHERE status='pending';
