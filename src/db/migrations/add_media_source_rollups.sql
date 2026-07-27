CREATE TABLE IF NOT EXISTS media_source_rollups (
    source VARCHAR(50) PRIMARY KEY,
    total_media_items BIGINT NOT NULL DEFAULT 0,
    total_media_bytes BIGINT NOT NULL DEFAULT 0,
    latest_media_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO media_source_rollups (source, total_media_items, total_media_bytes, latest_media_at, updated_at)
SELECT source,
       count(*)::bigint,
       COALESCE(sum(file_size), 0)::bigint,
       max(collected_at),
       NOW()
FROM media_items
GROUP BY source
ON CONFLICT (source) DO UPDATE SET
    total_media_items = EXCLUDED.total_media_items,
    total_media_bytes = EXCLUDED.total_media_bytes,
    latest_media_at = EXCLUDED.latest_media_at,
    updated_at = NOW();

CREATE OR REPLACE FUNCTION update_media_source_rollups()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO media_source_rollups (
            source, total_media_items, total_media_bytes, latest_media_at, updated_at
        )
        VALUES (
            NEW.source,
            1,
            COALESCE(NEW.file_size, 0),
            NEW.collected_at,
            NOW()
        )
        ON CONFLICT (source) DO UPDATE SET
            total_media_items = media_source_rollups.total_media_items + 1,
            total_media_bytes = media_source_rollups.total_media_bytes + COALESCE(NEW.file_size, 0),
            latest_media_at = GREATEST(
                COALESCE(media_source_rollups.latest_media_at, NEW.collected_at),
                NEW.collected_at
            ),
            updated_at = NOW();
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE media_source_rollups
        SET total_media_items = GREATEST(total_media_items - 1, 0),
            total_media_bytes = GREATEST(total_media_bytes - COALESCE(OLD.file_size, 0), 0),
            latest_media_at = CASE
                WHEN latest_media_at = OLD.collected_at THEN (
                    SELECT max(collected_at) FROM media_items WHERE source = OLD.source
                )
                ELSE latest_media_at
            END,
            updated_at = NOW()
        WHERE source = OLD.source;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.source = NEW.source
           AND COALESCE(OLD.file_size, 0) = COALESCE(NEW.file_size, 0)
           AND OLD.collected_at IS NOT DISTINCT FROM NEW.collected_at THEN
            RETURN NEW;
        END IF;

        UPDATE media_source_rollups
        SET total_media_bytes = GREATEST(total_media_bytes - COALESCE(OLD.file_size, 0), 0),
            latest_media_at = CASE
                WHEN latest_media_at = OLD.collected_at THEN (
                    SELECT max(collected_at) FROM media_items WHERE source = OLD.source AND id <> OLD.id
                )
                ELSE latest_media_at
            END,
            updated_at = NOW()
        WHERE source = OLD.source;

        INSERT INTO media_source_rollups (
            source, total_media_items, total_media_bytes, latest_media_at, updated_at
        )
        VALUES (
            NEW.source,
            CASE WHEN OLD.source = NEW.source THEN 0 ELSE 1 END,
            COALESCE(NEW.file_size, 0),
            NEW.collected_at,
            NOW()
        )
        ON CONFLICT (source) DO UPDATE SET
            total_media_items = media_source_rollups.total_media_items
                + CASE WHEN OLD.source = NEW.source THEN 0 ELSE 1 END,
            total_media_bytes = media_source_rollups.total_media_bytes + COALESCE(NEW.file_size, 0),
            latest_media_at = GREATEST(
                COALESCE(media_source_rollups.latest_media_at, NEW.collected_at),
                NEW.collected_at
            ),
            updated_at = NOW();

        IF OLD.source <> NEW.source THEN
            UPDATE media_source_rollups
            SET total_media_items = GREATEST(total_media_items - 1, 0),
                updated_at = NOW()
            WHERE source = OLD.source;
        END IF;
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_media_source_rollups ON media_items;
CREATE TRIGGER trg_media_source_rollups
AFTER INSERT OR UPDATE OR DELETE ON media_items
FOR EACH ROW EXECUTE FUNCTION update_media_source_rollups();
