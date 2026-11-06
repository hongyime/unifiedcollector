-- The recovery table was introduced after this deployment had already recorded
-- Instagram restriction responses. Preserve that real evidence once, without
-- overwriting profiles created by the live collector thereafter.
WITH latest_restriction AS (
    SELECT status_code, reason, created_at
    FROM rate_limit_events
    WHERE source = 'instagram'
      AND status_code IN (401, 403, 429)
      AND created_at >= now() - INTERVAL '7 days'
    ORDER BY created_at DESC
    LIMIT 1
)
INSERT INTO source_recovery_profiles (
    source_name, account_identity, state, reason, started_at,
    restricted_until, stale_since, clean_passes, restriction_count, updated_at
)
SELECT
    'instagram', '', 'restricted',
    LEFT('recovery rollout: historical HTTP ' || status_code || COALESCE(' (' || reason || ')', ''), 500),
    created_at,
    created_at + INTERVAL '45 hours',
    created_at,
    0,
    1,
    now()
FROM latest_restriction
ON CONFLICT (source_name, account_identity) DO NOTHING;
