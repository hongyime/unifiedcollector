CREATE TABLE IF NOT EXISTS browser_media_revisit_queue (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform text NOT NULL,
  content_id text NOT NULL,
  username text,
  post_url text,
  source_url text,
  reason text,
  status text NOT NULL DEFAULT 'pending',
  priority integer NOT NULL DEFAULT 50,
  attempts integer NOT NULL DEFAULT 0,
  next_visit_at timestamptz NOT NULL DEFAULT now(),
  last_attempt_at timestamptz,
  last_success_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_browser_media_revisit_platform_content
  ON browser_media_revisit_queue (platform, content_id);

CREATE INDEX IF NOT EXISTS idx_browser_media_revisit_due
  ON browser_media_revisit_queue (platform, status, next_visit_at, priority DESC);
