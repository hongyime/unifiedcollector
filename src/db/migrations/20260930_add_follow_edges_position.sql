-- 2026-09-30: capture the ordinal position of each follow edge within the
-- source's follow list at scrape time. Griffin's Threads OSINT post
-- (2024-10-10) notes the first-followed accounts are the highest-signal
-- ones for identity resolution: "the first account followed is all the
-- way at the bottom". Analyzer's cross_platform_link and real_name_fuzzy
-- signals can weight matches higher when they land in the low-position
-- band. NULL = unknown / legacy row / collector didn't record it.
--
-- Backfill is not attempted: position at time of NEW scrape is what we
-- want; historic rows stay NULL.
ALTER TABLE follow_edges
    ADD COLUMN IF NOT EXISTS follow_position INTEGER NULL;

CREATE INDEX IF NOT EXISTS idx_follow_edges_position
    ON follow_edges(platform, owner_account, direction, follow_position)
    WHERE follow_position IS NOT NULL;
