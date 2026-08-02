-- Avoid running the media source rollup trigger for metadata-only updates.
-- Hot paths such as vault sidecar status/consistency checks update media_items.metadata
-- after the row is inserted; those writes do not change counts, bytes, or freshness.
DROP TRIGGER IF EXISTS trg_media_source_rollups ON media_items;

CREATE TRIGGER trg_media_source_rollups
AFTER INSERT OR DELETE OR UPDATE OF source, file_size, collected_at ON media_items
FOR EACH ROW EXECUTE FUNCTION update_media_source_rollups();
