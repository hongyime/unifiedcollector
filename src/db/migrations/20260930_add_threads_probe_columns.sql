-- 2026-09-30: track when we last probed a threads.net presence for each
-- Instagram profile. Griffin's "Threads OSINT Secrets" (2024-10-10):
-- threads.net/@<ig_username> resolves even when IG doesn't link to it,
-- and Threads bio + photo may differ from IG. NULL threads_probe_at =
-- never probed. The threads_probe_* fields are separate from any
-- threads_* content we already have via ig_ingest browser bridge.
--
-- Stale-first index: the analyzer pipeline (Do Now #4) picks the next
-- batch by "oldest probe first, NULL first".
ALTER TABLE instagram_profiles
    ADD COLUMN IF NOT EXISTS threads_probe_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS threads_probe_exists BOOLEAN NULL,
    ADD COLUMN IF NOT EXISTS threads_probe_bio TEXT NULL,
    ADD COLUMN IF NOT EXISTS threads_probe_avatar_url TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_instagram_profiles_threads_probe_stale
    ON instagram_profiles(threads_probe_at NULLS FIRST);
