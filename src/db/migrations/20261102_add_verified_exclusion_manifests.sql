CREATE TABLE IF NOT EXISTS verified_exclusion_manifests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_verified_exclusion_manifests_created
    ON verified_exclusion_manifests (created_at DESC);
