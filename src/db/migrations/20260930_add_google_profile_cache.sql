-- 2026-09-30: cache of Google-profile lookups keyed on email.
-- Populated by src/pipeline/epieos_probe.py. Mirrors the whois_cache
-- pattern: one row per email, refreshed when past stale window.
-- Reviews go into maps_reviews JSONB as an array of
-- {place_id, place_name, rating, text, posted_at, lat, lng} objects;
-- we don't need a full normalized table for v1.
--
-- Griffin's demonstrated payoff (Scam-a-Scammer 2023-05-18): reviews
-- geocluster and reveal where the target lives, works, and frequents.
CREATE TABLE IF NOT EXISTS google_profile_cache (
    email               text        PRIMARY KEY,
    gaia_id             text        NULL,
    display_name        text        NULL,
    avatar_url          text        NULL,
    has_profile         boolean     NOT NULL,
    maps_reviews        jsonb       NULL,
    maps_photos_urls    text[]      NULL,
    raw_payload         jsonb       NULL,
    source              text        NOT NULL DEFAULT 'gaia',
    fetched_at          timestamptz NOT NULL DEFAULT now(),
    stale_after         timestamptz NULL,
    last_error          text        NULL
);

CREATE INDEX IF NOT EXISTS idx_google_profile_cache_gaia
    ON google_profile_cache(gaia_id)
    WHERE gaia_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_google_profile_cache_stale
    ON google_profile_cache(stale_after)
    WHERE stale_after IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_google_profile_cache_has_profile
    ON google_profile_cache(has_profile) WHERE has_profile = true;
