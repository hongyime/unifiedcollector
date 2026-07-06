-- 2026-07-06 (P1.2): passive telemetry for the DM WS hooks in the browser
-- extension. dm_probe_handler and dm_sample_handler in src/bridges/ig_ingest.py
-- insert one row per event so the dashboard can show at a glance whether real
-- DM frames are landing per platform (the whole point being to know when real
-- Instagram DM samples finally arrive — right now IG is stuck at 1 placeholder
-- while TikTok is at 61 real protobuf samples).
--
-- event_type distinguishes:
--   'probe'  — one row per distinct socket URL the extension's WS wrapper saw
--   'sample' — one row per raw binary frame ≥ SAMPLE_MIN_BYTES that got saved
--              to /tmp/dm_samples/ for decoder work
--
-- Retention is not enforced here — the table is thin (bigint + a few TEXTs)
-- and rows arrive at O(samples/hour), so growth is negligible for months. If
-- it ever matters, add a housekeeping job that DELETEs seen_at < now() - '30d'.
CREATE TABLE IF NOT EXISTS dm_probe_log (
    id            BIGSERIAL PRIMARY KEY,
    platform      TEXT        NOT NULL,
    event_type    TEXT        NOT NULL DEFAULT 'probe',
    url           TEXT,
    transport     TEXT,
    frame_kind    TEXT,
    frame_size    INT,
    owner_account TEXT,
    seen_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Covers the two hot dashboard queries: "per-platform stats" and "last-N events".
CREATE INDEX IF NOT EXISTS idx_dm_probe_log_platform_seen
    ON dm_probe_log (platform, seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_dm_probe_log_event_seen
    ON dm_probe_log (event_type, seen_at DESC);
