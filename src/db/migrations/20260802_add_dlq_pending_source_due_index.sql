-- Match DLQConsumer._claim_batch: pending rows by handled source, ordered by
-- next_retry_at, then locked by id. Keeps retry scans cheap when media repair or
-- sidecar failures produce a large pending queue.
CREATE INDEX IF NOT EXISTS idx_dlq_pending_source_due
    ON dead_letter_queue (source, next_retry_at, id)
    WHERE status = 'pending';
