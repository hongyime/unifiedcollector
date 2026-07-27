-- Fast health/metrics count for media rows whose sidecar metadata explicitly failed.
CREATE INDEX IF NOT EXISTS idx_media_partial_sidecar_failure
    ON media_items (collected_at DESC)
    WHERE (
        metadata ? 'vault_sidecar'
        AND metadata->'vault_sidecar'->>'ok' = 'false'
    ) OR (
        metadata ? 'vault_artifact'
        AND (
            metadata->'vault_artifact'->>'ok' = 'false'
            OR metadata->'vault_artifact'->>'sidecar_ok' = 'false'
            OR metadata->'vault_artifact'->>'partial' = 'true'
        )
    );
