-- Release the parked Website backlog without removing targets, pages, media,
-- or error history.  The attempt rows are reset in the same transaction so a
-- newly pending target cannot be skipped by stale backoff state.
BEGIN;

UPDATE collection_targets
   SET status = 'pending', error_message = NULL, last_collection_at = NULL
 WHERE source = 'website' AND status = 'error';

UPDATE website_targets
   SET status = 'pending', updated_at = NOW()
 WHERE status = 'error';

DELETE FROM website_target_attempts
 WHERE target_id IN (
    SELECT target_id FROM collection_targets WHERE source = 'website'
 );

COMMIT;
