-- 2026-07-06 (P1.3): heartbeat table for the browser-side DM WebSocket hook.
--
-- The extension's inject.js wraps window.WebSocket in a passive send-nothing
-- wrapper (see extension/inject.js "DM investigation" block). It's installed
-- once at page load; if Instagram/TikTok update their bundle and the hook
-- silently breaks, we currently have no way to detect that — DM samples just
-- stop arriving and it looks identical to "user isn't DMing". P1.3 closes that
-- gap: every N minutes the hook POSTs a heartbeat with its running counters,
-- and the freshness watchdog alerts on Telegram if the newest heartbeat per
-- platform goes stale (no restart possible — the hook lives in the browser).
--
-- PK is (platform, owner_account). owner_account is NOT NULL DEFAULT '' so
-- the composite key can't accept a NULL row (Postgres treats NULLs in a PK as
-- allowed-and-distinct, which would defeat upsert-on-conflict). Empty string
-- means "extension couldn't derive the logged-in account", which is legitimate
-- on TikTok (no simple ds_user_id-style cookie).
CREATE TABLE IF NOT EXISTS dm_hook_heartbeat (
    platform          TEXT        NOT NULL,
    owner_account     TEXT        NOT NULL DEFAULT '',
    last_seen         TIMESTAMPTZ NOT NULL DEFAULT now(),
    probes_sent       BIGINT      NOT NULL DEFAULT 0,
    samples_shipped   BIGINT      NOT NULL DEFAULT 0,
    extension_version TEXT,
    user_agent        TEXT,
    PRIMARY KEY (platform, owner_account)
);

-- Watchdog / dashboard query: newest heartbeat per platform.
CREATE INDEX IF NOT EXISTS idx_dm_hook_heartbeat_platform_last_seen
    ON dm_hook_heartbeat (platform, last_seen DESC);
