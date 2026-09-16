-- Fresh schemas lack fields already used by the Lemon8 writer and historical
-- profile backfill. Add nullable columns without rewriting existing rows or
-- changing any previously applied migration and its checksum.
ALTER TABLE lemon8_posts
    ADD COLUMN IF NOT EXISTS username VARCHAR(255),
    ADD COLUMN IF NOT EXISTS post_url TEXT;
