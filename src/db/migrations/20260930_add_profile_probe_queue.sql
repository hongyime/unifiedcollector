-- 2026-09-30: shared queue for lightweight profile-only collectors
-- (Do Next #3). Populated by handle_fanout, operator UI, and other
-- collectors when they encounter a username on a platform we don't
-- automate through a full worker loop (snapchat/paypal/airbnb/bluesky/
-- pinterest). Consumed by the single collector_profile_only container.
--
-- Round-robin friendly: source ordering ensures we don't starve one
-- platform when another is backlogged.
CREATE TABLE IF NOT EXISTS profile_probe_queue (
    id            bigserial   PRIMARY KEY,
    source        text        NOT NULL,   -- 'snapchat' | 'paypal' | 'airbnb' | 'bluesky' | 'pinterest'
    username      text        NOT NULL,
    status        text        NOT NULL DEFAULT 'pending',   -- pending | done | 404 | error
    added_at      timestamptz NOT NULL DEFAULT now(),
    probed_at     timestamptz NULL,
    error_message text        NULL,
    enqueued_by   text        NULL,       -- audit trail: which pipeline queued it
    UNIQUE (source, username)
);
CREATE INDEX IF NOT EXISTS idx_profile_probe_queue_pending
    ON profile_probe_queue(source, status, added_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_profile_probe_queue_probed
    ON profile_probe_queue(probed_at DESC)
    WHERE status IN ('done', '404', 'error');
