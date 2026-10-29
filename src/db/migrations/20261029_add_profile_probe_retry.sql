-- 2026-10-29: bounded retry for profile_probe_queue.
--
-- Transient probe failures (timeouts, 5xx, 429, challenge/payload errors) used
-- to be terminal, so one blip permanently dropped a target. They now re-enter
-- the queue with exponential backoff; the collector's PROFILE_PROBE_MAX_ATTEMPTS
-- (default 5) still terminates a target that keeps failing.
--
-- ADD COLUMN IF NOT EXISTS keeps this safe to re-run by hand; the migration
-- runner additionally applies each file once (ledger + checksum).
ALTER TABLE profile_probe_queue
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_probe_at TIMESTAMPTZ;

-- Selection is `status='pending' AND (next_probe_at IS NULL OR next_probe_at
-- <= NOW())` scoped to one source. The partial index keeps the due-time filter
-- from scanning the backlog of rows that are not yet due.
CREATE INDEX IF NOT EXISTS idx_profile_probe_queue_due
    ON profile_probe_queue(source, next_probe_at, added_at)
    WHERE status = 'pending';
