-- Fast health/repair checks for the atomic artifact sidecar metadata shape.
CREATE INDEX IF NOT EXISTS idx_media_vault_artifact_ok
    ON media_items ((metadata->'vault_artifact'->>'ok'))
    WHERE metadata ? 'vault_artifact';

CREATE INDEX IF NOT EXISTS idx_media_vault_artifact_sidecar_path
    ON media_items ((metadata->'vault_artifact'->>'sidecar_path'))
    WHERE metadata ? 'vault_artifact';

CREATE INDEX IF NOT EXISTS idx_media_missing_occurrence_sidecar
    ON media_items (collected_at DESC)
    WHERE file_path IS NOT NULL
      AND file_path <> ''
      AND content_id IS NOT NULL
      AND content_id <> ''
      AND NOT (COALESCE(metadata, '{}'::jsonb) ? 'vault_sidecar')
      AND NOT (
          COALESCE(metadata, '{}'::jsonb) ? 'vault_artifact'
          AND COALESCE(metadata->'vault_artifact'->>'sidecar_path', '') <> ''
      );
