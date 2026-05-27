-- Migration: add unified spider_queue table
-- Created for src/core/spider_discover.py (Wave 0 cross-cutting module)
--
-- Generalises the per-platform *_spider_queue tables (github_spider_queue,
-- instagram_spider_queue, lemon8_spider_queue, ...) into a single keyed-by-
-- platform table, used by the new SpiderDiscover engine to drive BFS
-- follower/following graph traversal across 6 platforms.
--
-- This migration is idempotent — safe to run multiple times. Reverse with
-- the corresponding `down` block at the bottom (commented out by default;
-- uncomment to roll back during testing).
--
-- Composite PK (platform, node_id) — one row per node per platform. Status
-- transitions: pending -> in_progress -> completed | failed. The hop_distance
-- column is the BFS depth from the seed; priority is a secondary ordering
-- key (lower number = higher priority) so callers can boost specific nodes.

CREATE TABLE IF NOT EXISTS spider_queue (
    platform           VARCHAR(50)  NOT NULL,
    node_id            VARCHAR(500) NOT NULL,
    hop_distance       INT          NOT NULL DEFAULT 0,
    priority           INT          NOT NULL DEFAULT 5,
    status             VARCHAR(20)  NOT NULL DEFAULT 'pending',
    parent_node_id     VARCHAR(500),
    edge_type          VARCHAR(50),  -- which edge type led here (follower/following/...)
    enqueued_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_attempted_at  TIMESTAMPTZ,
    completed_at       TIMESTAMPTZ,
    attempts           INT          NOT NULL DEFAULT 0,
    error              TEXT,
    PRIMARY KEY (platform, node_id),
    CONSTRAINT spider_queue_status_chk
        CHECK (status IN ('pending', 'in_progress', 'completed', 'failed'))
);

-- Pull-next index: claim by platform, status=pending, ordered by hop then priority
CREATE INDEX IF NOT EXISTS idx_spider_queue_pull
    ON spider_queue (platform, status, hop_distance, priority, enqueued_at);

-- Status-only sweep (e.g. resetting stuck in_progress on restart)
CREATE INDEX IF NOT EXISTS idx_spider_queue_status
    ON spider_queue (status, last_attempted_at);

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_spider_queue_status;
-- DROP INDEX IF EXISTS idx_spider_queue_pull;
-- DROP TABLE IF EXISTS spider_queue;
