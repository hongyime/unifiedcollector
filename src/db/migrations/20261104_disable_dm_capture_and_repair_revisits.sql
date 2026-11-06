-- Direct-message retention ends with the deployed capture shutdown.  Keep this
-- destructive work narrowly scoped: ordinary social rows and media are not DM.
DO $$
DECLARE
    dm_before bigint := 0;
    thread_before bigint := 0;
    probe_before bigint := 0;
    heartbeat_before bigint := 0;
    dlq_before bigint := 0;
BEGIN
    SELECT count(*) INTO dm_before FROM tiktok_dm;
    SELECT count(*) INTO thread_before FROM tiktok_dm_thread;
    SELECT count(*) INTO probe_before FROM dm_probe_log;
    SELECT count(*) INTO heartbeat_before FROM dm_hook_heartbeat;
    SELECT count(*) INTO dlq_before
    FROM dead_letter_queue
    WHERE source IN ('dm', 'direct_message')
       OR content_id LIKE 'raw:extension/dm_%'
       OR lower(COALESCE(error_message, '')) LIKE '%dm_%';

    DELETE FROM tiktok_dm;
    DELETE FROM tiktok_dm_thread;
    DELETE FROM dm_hook_heartbeat;
    DELETE FROM dm_probe_log;
    DELETE FROM dead_letter_queue
    WHERE source IN ('dm', 'direct_message')
       OR content_id LIKE 'raw:extension/dm_%'
       OR lower(COALESCE(error_message, '')) LIKE '%dm_%';

    IF to_regclass('instagram_dm') IS NOT NULL THEN EXECUTE 'DELETE FROM instagram_dm'; END IF;
    IF to_regclass('instagram_dm_thread') IS NOT NULL THEN EXECUTE 'DELETE FROM instagram_dm_thread'; END IF;
    RAISE LOG 'DM purge: tiktok_dm %, threads %, probes %, heartbeats %, DLQ %',
        dm_before, thread_before, probe_before, heartbeat_before, dlq_before;
END $$;

-- A completed revisit with no stored artifact was a false positive.  Give it
-- one normal visible-page revisit; the usual bounded retry policy owns the rest.
UPDATE tiktok_browser_revisit_queue
SET status = 'pending',
    reason = 'historical_zero_store_revisit',
    attempts = 0,
    next_visit_at = now(),
    last_success_at = NULL,
    metadata = metadata || jsonb_build_object('historical_zero_store_reopened_at', now()),
    updated_at = now()
WHERE status = 'completed'
  AND COALESCE((metadata->'last_result'->>'stored')::int, 0) = 0;

-- Raw archive writes have no replayable Threads media input.  Make them
-- terminal so the DLQ watchdog counts only work the repair path can perform.
UPDATE dead_letter_queue
SET status = 'failed',
    error_message = 'terminal historical raw archive: original payload unavailable',
    next_retry_at = now(),
    last_attempt_at = now()
WHERE source = 'threads'
  AND content_id LIKE 'raw:%'
  AND lower(COALESCE(error_message, '')) LIKE 'raw payload archive failed:%';
