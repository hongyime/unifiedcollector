-- 2026-07-03: bounded retry + failure taxonomy for the telegram spider queue.
-- The old _process_spider_queue marked ANY exception as 'failed' with no retry and
-- no reason, so ~8.7k chats were terminally failed — many by TRANSIENT blips
-- (disconnects/floods on 06-26) rather than being genuinely unresolvable. Now:
--   * attempts   – incremented on transient/unknown failure; re-queued as 'pending'
--                  until TELEGRAM_SPIDER_MAX_ATTEMPTS, then 'failed'.
--   * last_error – the last failure reason (for triage).
--   * status 'unresolvable' (no schema change needed — status is free-text) means
--                  every account was asked and none owns the chat (left/deleted/
--                  private); terminal, so it doesn't churn the queue.
-- Idempotent: ADD COLUMN IF NOT EXISTS.
ALTER TABLE telegram_spider_queue
    ADD COLUMN IF NOT EXISTS attempts   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE telegram_spider_queue
    ADD COLUMN IF NOT EXISTS last_error TEXT;
