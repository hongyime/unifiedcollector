-- Fast health/metrics counts for vault sidecar status.
CREATE INDEX IF NOT EXISTS idx_media_vault_sidecar_ok
    ON media_items ((metadata->'vault_sidecar'->>'ok'))
    WHERE metadata ? 'vault_sidecar';

CREATE INDEX IF NOT EXISTS idx_dlq_vault_sidecar_failures
    ON dead_letter_queue (status, created_at)
    WHERE error_message LIKE 'vault sidecar write failed:%';
