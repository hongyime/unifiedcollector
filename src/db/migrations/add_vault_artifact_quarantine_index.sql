-- Fast exact count for quarantined vault artifacts in health/status reports.
CREATE INDEX IF NOT EXISTS idx_media_vault_artifact_quarantined
    ON media_items ((metadata->'vault_artifact'->>'quarantined'))
    WHERE metadata ? 'vault_artifact';
