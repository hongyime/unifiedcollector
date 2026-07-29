-- Keyset-friendly indexes for bounded historical media repair jobs.
CREATE INDEX IF NOT EXISTS idx_media_missing_occurrence_sidecar_source_content
    ON media_items (source, content_id)
    WHERE file_path IS NOT NULL
      AND file_path <> ''
      AND content_id IS NOT NULL
      AND content_id <> ''
      AND NOT (COALESCE(metadata, '{}'::jsonb) ? 'vault_sidecar')
      AND NOT (
          COALESCE(metadata, '{}'::jsonb) ? 'vault_artifact'
          AND COALESCE(metadata->'vault_artifact'->>'sidecar_path', '') <> ''
      );

CREATE INDEX IF NOT EXISTS idx_media_partial_sidecar_failure_source_content
    ON media_items (source, content_id)
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
