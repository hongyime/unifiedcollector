-- 2026-07-04: persisted per-account cookie validity so the dashboard can show
-- "refresh needed" without live-probing (which is ban-sensitive for IG). The
-- collectors already test cookies every cycle — they mark an account dead on a
-- 401 and clear it when the cookie file is refreshed — so we just persist that
-- signal here for the /accounts panel to read.
CREATE TABLE IF NOT EXISTS cookie_status (
    platform   text NOT NULL,
    account    text NOT NULL,           -- cookie account name (usually the username)
    status     text NOT NULL DEFAULT 'unknown',   -- ok | dead | unknown
    reason     text,
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, account)
);
