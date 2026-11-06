CREATE TABLE IF NOT EXISTS verified_exclusion_purge_rows (
    id BIGSERIAL PRIMARY KEY,
    receipt_id UUID NOT NULL REFERENCES verified_exclusion_purge_receipts(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    entity TEXT NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_verified_exclusion_purge_rows_receipt
    ON verified_exclusion_purge_rows (receipt_id, source, entity);

CREATE INDEX IF NOT EXISTS idx_media_items_file_path
    ON media_items (file_path);

CREATE INDEX IF NOT EXISTS idx_media_items_sha256_nonempty
    ON media_items (sha256)
    WHERE sha256 IS NOT NULL AND sha256 <> '';
