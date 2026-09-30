-- 2026-09-30: TikTok post-ID -> UTC timestamp decoder column.
-- IDs are 64-bit numbers whose high 32 bits are seconds since the
-- TikTok epoch (2016-06-01 00:00:00 UTC). Decoded at insert time by
-- the collector, and backfilled for existing rows via
-- scripts/backfill_tiktok_id_utc.py. Source: Bellingcat, surfaced in
-- Griffin's tools.myosint.training "TikTok URL Date Decoder".
ALTER TABLE tiktok_posts
    ADD COLUMN IF NOT EXISTS id_decoded_utc TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_tiktok_posts_id_decoded_utc
    ON tiktok_posts(id_decoded_utc)
    WHERE id_decoded_utc IS NOT NULL;
