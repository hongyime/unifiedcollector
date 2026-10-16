ALTER TABLE telegram_spider_queue
    ADD COLUMN IF NOT EXISTS discussion_checked_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_tg_spider_discussion_sweep
    ON telegram_spider_queue (
        priority ASC,
        discussion_checked_at ASC NULLS FIRST,
        collected_at ASC,
        id
    ) INCLUDE (platform_chat_id)
    WHERE status = 'pending';
