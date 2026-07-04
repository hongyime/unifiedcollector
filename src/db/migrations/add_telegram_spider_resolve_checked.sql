-- 2026-07-04: resolve-only sweep marker for the telegram spider queue.
-- The drain does a FULL history backfill per chat, one at a time per worker, so a
-- few huge channels monopolize all workers for hours and the genuinely-dead chats
-- (left/deleted/private) sit behind them, never getting reclassified. The resolve
-- sweep cheaply checks each pending chat against all accounts (get_entity only, no
-- message pull) and marks the dead ones 'unresolvable' immediately.
-- resolve_checked_at lets the sweep advance through the queue instead of
-- re-resolving the same resolvable chats every cycle: set on a chat once it
-- resolves OK (leaving it 'pending' for the drain), so the next sweep skips it.
-- Idempotent.
ALTER TABLE telegram_spider_queue
    ADD COLUMN IF NOT EXISTS resolve_checked_at TIMESTAMPTZ;

-- Sweep picks unchecked pending chats first.
CREATE INDEX IF NOT EXISTS idx_tg_spider_resolve_sweep
    ON telegram_spider_queue (resolve_checked_at)
    WHERE status = 'pending';
