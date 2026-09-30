-- 2026-09-30: well-known files scanner storage (Explore #4).
-- Populated by src/collectors/exposure/well_known_scanner.py on a
-- weekly schedule for domains that recur in discovered_links.
-- Griffin's tools.myosint.training "Root .txt Scanner" bookmarklet
-- surfaces contact emails, ad-tech partners, and OIDC discovery info
-- from robots.txt / security.txt / sitemap.xml / .well-known/*
-- without any client-side auth.
--
-- Findings are the raw file contents; extracted_data is a structured
-- JSONB of what we mined (emails / phones / links / IDs).

CREATE TABLE IF NOT EXISTS well_known_findings (
    domain           text        NOT NULL,
    file_path        text        NOT NULL,  -- 'robots.txt' | 'security.txt' | 'sitemap.xml' | ...
    fetched_at       timestamptz NOT NULL DEFAULT now(),
    http_status      integer     NOT NULL,
    content_hash     bytea       NULL,
    content          text        NULL,
    extracted_data   jsonb       NULL,
    PRIMARY KEY (domain, file_path, fetched_at)
);

CREATE INDEX IF NOT EXISTS idx_well_known_findings_domain
    ON well_known_findings(domain, file_path);
CREATE INDEX IF NOT EXISTS idx_well_known_findings_emails
    ON well_known_findings USING gin ((extracted_data->'emails'))
    WHERE extracted_data ? 'emails';

-- Scheduling table: which domains are due for a scan, priority-based.
CREATE TABLE IF NOT EXISTS well_known_scan_queue (
    domain       text        PRIMARY KEY,
    priority     integer     NOT NULL DEFAULT 0,   -- higher = scan first
    last_scanned timestamptz NULL,
    next_scan    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_well_known_scan_queue_next
    ON well_known_scan_queue(next_scan);
