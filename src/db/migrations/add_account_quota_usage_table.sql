-- Migration: add per-account quota usage tracking table
-- Created for src/core/account_quota.py (Wave 0 batch 2 — cross-cutting module)
--
-- Tracks per-(platform, account, day) request counts so AccountPool can
-- decide whether to dispense an account or mark it 'quota_exhausted'.
--
-- One row per (platform, account, day) where day = SGT calendar date
-- (i.e. (now_utc + 8h)::date). Hourly counter is stored alongside with a
-- string hour_bucket key; the writer resets it when the bucket rolls.
-- Weekly counter is denormalised on each row (= SUM of today's counters
-- for the same ISO week) for fast reads; the SUM is recomputed on every
-- consume so it is eventually consistent across rows in the same week.
--
-- This migration is idempotent — safe to re-run. Reverse with the down
-- block at the bottom (commented).

CREATE TABLE IF NOT EXISTS account_quota_usage (
    platform        VARCHAR(50)  NOT NULL,
    account         VARCHAR(255) NOT NULL,
    day             DATE         NOT NULL,
    requests_today  BIGINT       NOT NULL DEFAULT 0,
    week_iso        VARCHAR(10)  NOT NULL,    -- e.g. '2026-W22'
    requests_week   BIGINT       NOT NULL DEFAULT 0,
    hour_bucket     VARCHAR(20)  NOT NULL,    -- 'YYYY-MM-DD HH:00' SGT
    requests_hour   BIGINT       NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (platform, account, day),
    CONSTRAINT account_quota_usage_today_chk    CHECK (requests_today >= 0),
    CONSTRAINT account_quota_usage_week_chk     CHECK (requests_week >= 0),
    CONSTRAINT account_quota_usage_hour_chk     CHECK (requests_hour >= 0)
);

-- Hot-path lookup: (platform, account, day) is already the PK; this index
-- supports the "SUM(requests_today) WHERE platform=? AND account=? AND
-- week_iso=?" rollup used in get_usage / consume.
CREATE INDEX IF NOT EXISTS idx_account_quota_usage_week
    ON account_quota_usage (platform, account, week_iso);

-- Reporting / housekeeping: scan a whole platform on a given day.
CREATE INDEX IF NOT EXISTS idx_account_quota_usage_platform_day
    ON account_quota_usage (platform, day);

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_account_quota_usage_platform_day;
-- DROP INDEX IF EXISTS idx_account_quota_usage_week;
-- DROP TABLE IF EXISTS account_quota_usage;
