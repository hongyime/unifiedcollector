CREATE TABLE IF NOT EXISTS browser_media_candidates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform text NOT NULL,
  content_id text NOT NULL,
  username text,
  source_url text,
  url_hash text NOT NULL,
  asset_role text,
  content_type text,
  width integer,
  height integer,
  file_size bigint,
  mime_type text,
  extension_version text,
  ingest_mode text NOT NULL DEFAULT 'url',
  outcome text NOT NULL DEFAULT 'observed',
  reason text,
  needs_revisit boolean NOT NULL DEFAULT false,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  first_seen timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now(),
  attempts integer NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_browser_media_candidate
  ON browser_media_candidates (platform, content_id, url_hash, ingest_mode);
CREATE INDEX IF NOT EXISTS idx_browser_media_candidates_platform_seen
  ON browser_media_candidates (platform, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_browser_media_candidates_platform_outcome
  ON browser_media_candidates (platform, outcome, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_browser_media_candidates_revisit
  ON browser_media_candidates (platform, needs_revisit, last_seen DESC)
  WHERE needs_revisit;
