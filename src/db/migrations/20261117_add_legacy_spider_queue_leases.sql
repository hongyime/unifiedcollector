-- Fence legacy source-specific spider queues against overlapping workers.
-- Existing in-flight rows receive a short rollout lease rather than being
-- immediately reset by a newly deployed worker.
ALTER TABLE youtube_spider_queue
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claim_token UUID;

ALTER TABLE github_spider_queue
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claim_token UUID;

ALTER TABLE strava_spider_queue
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claim_token UUID;

ALTER TABLE lemon8_spider_queue
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claim_token UUID;

UPDATE youtube_spider_queue
   SET claimed_at = COALESCE(collected_at, NOW()),
       lease_expires_at = NOW() + INTERVAL '30 minutes'
 WHERE status = 'processing' AND lease_expires_at IS NULL;

UPDATE github_spider_queue
   SET claimed_at = COALESCE(collected_at, NOW()),
       lease_expires_at = NOW() + INTERVAL '30 minutes'
 WHERE status = 'processing' AND lease_expires_at IS NULL;

UPDATE strava_spider_queue
   SET claimed_at = COALESCE(collected_at, NOW()),
       lease_expires_at = NOW() + INTERVAL '30 minutes'
 WHERE status = 'processing' AND lease_expires_at IS NULL;

UPDATE lemon8_spider_queue
   SET claimed_at = COALESCE(collected_at, NOW()),
       lease_expires_at = NOW() + INTERVAL '30 minutes'
 WHERE status = 'processing' AND lease_expires_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_youtube_spider_queue_expired_lease
    ON youtube_spider_queue (lease_expires_at) WHERE status = 'processing';
CREATE INDEX IF NOT EXISTS idx_github_spider_queue_expired_lease
    ON github_spider_queue (lease_expires_at) WHERE status = 'processing';
CREATE INDEX IF NOT EXISTS idx_strava_spider_queue_expired_lease
    ON strava_spider_queue (lease_expires_at) WHERE status = 'processing';
CREATE INDEX IF NOT EXISTS idx_lemon8_spider_queue_expired_lease
    ON lemon8_spider_queue (lease_expires_at) WHERE status = 'processing';
