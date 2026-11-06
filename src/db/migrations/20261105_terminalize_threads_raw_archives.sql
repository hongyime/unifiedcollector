-- Legacy browser raw-archive failures have no replay payload in the DLQ schema.
-- Terminalize only that unrecoverable Threads subset; normal repairable media stays queued.
UPDATE dead_letter_queue
SET status = 'failed',
    error_message = 'terminal historical browser raw archive: replay payload unavailable',
    next_retry_at = now(),
    last_attempt_at = now()
WHERE source = 'threads'
  AND status <> 'failed'
  AND lower(COALESCE(error_message, '')) LIKE 'browser raw archive failed:%';
