-- A worker heartbeat is not proof that its upstream was reachable.  Keep the
-- check and content clocks separate so quiet sources remain observable.
ALTER TABLE source_health
    ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_content_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS freshness_state TEXT,
    ADD COLUMN IF NOT EXISTS blocker TEXT;

UPDATE source_health
   SET last_checked_at = COALESCE(last_checked_at, last_success_at),
       freshness_state = COALESCE(freshness_state, CASE
           WHEN status IN ('dead', 'degraded', 'auth_paused') THEN 'degraded'
           WHEN last_success_at IS NOT NULL THEN 'checked'
           ELSE 'unknown'
       END),
       blocker = COALESCE(blocker, last_error);

CREATE INDEX IF NOT EXISTS idx_source_health_last_checked_at
    ON source_health (last_checked_at);
