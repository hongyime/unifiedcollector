-- 2026-09-30: shared WHOIS cache. Enrichment via Whoxy or equivalent
-- runs once per unique registrable domain, cached indefinitely and
-- refreshed on operator demand. Consumers: discovered_links, exposure
-- collector, future domain-pivot flows. Sourced from Griffin's
-- "Beyond WHOIS" (2023-09-25) + "Art of Pivoting" (2022-04-27).
CREATE TABLE IF NOT EXISTS whois_cache (
    domain              text        PRIMARY KEY,     -- registrable form, e.g. "example.com"
    owner_name          text        NULL,
    owner_email         text        NULL,
    owner_organization  text        NULL,
    registrar           text        NULL,
    registered_at       timestamptz NULL,
    expires_at          timestamptz NULL,
    privacy_flag        boolean     NOT NULL DEFAULT false,
    ns_hosts            text[]      NULL,
    raw_payload         jsonb       NULL,
    source              text        NOT NULL DEFAULT 'whoxy',
    fetched_at          timestamptz NOT NULL DEFAULT now(),
    stale_after         timestamptz NULL
);

CREATE INDEX IF NOT EXISTS idx_whois_cache_owner_email
    ON whois_cache(owner_email)
    WHERE owner_email IS NOT NULL AND NOT privacy_flag;

CREATE INDEX IF NOT EXISTS idx_whois_cache_owner_name
    ON whois_cache(lower(owner_name))
    WHERE owner_name IS NOT NULL AND NOT privacy_flag;

CREATE INDEX IF NOT EXISTS idx_whois_cache_ns
    ON whois_cache USING gin(ns_hosts)
    WHERE ns_hosts IS NOT NULL;
