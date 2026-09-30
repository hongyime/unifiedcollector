-- 2026-09-30: archive snapshots for deleted/changed content (Explore #3).
-- Wayback-first fallback per Griffin's Telegram OSINT posts (2022-06-24
-- / 2022-08-03). When a collector detects a deleted/404'd resource, it
-- asks the archive_fallback pipeline to look for a preserved snapshot.
-- Snapshots stored gzipped inline (BYTEA) - each Telegram channel
-- snapshot is 5-20 KB compressed, so 10k snapshots = ~200 MB.
--
-- source_authority is DELIBERATELY separate from any live table's row -
-- archive data does NOT overwrite live data, ever. Callers must know
-- they're reading an archive snapshot.

CREATE TABLE IF NOT EXISTS archive_snapshots (
    id                 bigserial   PRIMARY KEY,
    source_table       text        NOT NULL,   -- 'telegram_messages' | 'instagram_profiles' | ...
    source_record_id   text        NOT NULL,
    archive_service    text        NOT NULL,   -- 'wayback' | 'archive_today' | 'google_cache'
    snapshot_url       text        NOT NULL,
    snapshot_captured_at timestamptz NULL,
    content_hash       bytea       NULL,
    raw_html_gzip      bytea       NULL,
    fetched_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_table, source_record_id, archive_service, snapshot_captured_at)
);
CREATE INDEX IF NOT EXISTS idx_archive_snapshots_source
    ON archive_snapshots(source_table, source_record_id);

-- Queue for domains/resources awaiting an archive lookup. Callers append
-- rows when they detect a 404 or similar; the fallback pipeline drains
-- this queue on its own schedule.
CREATE TABLE IF NOT EXISTS archive_fallback_queue (
    id                 bigserial   PRIMARY KEY,
    source_table       text        NOT NULL,
    source_record_id   text        NOT NULL,
    target_url         text        NOT NULL,
    added_at           timestamptz NOT NULL DEFAULT now(),
    status             text        NOT NULL DEFAULT 'pending',   -- pending | done | not_found | error
    processed_at       timestamptz NULL,
    error_message      text        NULL,
    UNIQUE (source_table, source_record_id, target_url)
);
CREATE INDEX IF NOT EXISTS idx_archive_fallback_queue_pending
    ON archive_fallback_queue(status, added_at)
    WHERE status = 'pending';
