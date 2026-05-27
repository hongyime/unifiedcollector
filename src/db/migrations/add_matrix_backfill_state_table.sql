-- Migration: add matrix_backfill_state table
-- Wave 1 Phase 2 — historical backfill cursor + progress per Matrix room.
--
-- The forward-sync collector (Phase 1) handles new events streaming in via
-- /sync. THIS table tracks the parallel backfill driver
-- (`src.core.matrix_backfill.MatrixBackfillDriver`) that walks each room
-- BACKWARDS via /rooms/{id}/messages?dir=b, page by page, until either
-- target_depth events are ingested per room or the homeserver reports
-- end-of-history.
--
-- The driver is resumable: on each /messages response we update last_token
-- + counters; on natural completion we set done=TRUE so future cycles
-- skip the room entirely. The partial index over WHERE done=FALSE keeps
-- "pending rooms" lookups fast even when most rooms are complete.
--
-- Idempotent — safe to run repeatedly.

CREATE TABLE IF NOT EXISTS matrix_backfill_state (
    room_id          VARCHAR(256) PRIMARY KEY,
    last_token       TEXT,                            -- pagination token from /messages
    earliest_ts      TIMESTAMPTZ,                     -- oldest event we've ingested for this room
    events_fetched   INTEGER NOT NULL DEFAULT 0,
    pages_used       INTEGER NOT NULL DEFAULT 0,
    done             BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE when /messages returned end-of-history
    last_error       TEXT,
    last_attempt_at  TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_matrix_backfill_done
    ON matrix_backfill_state (done) WHERE done = FALSE;

-- ---------------------------------------------------------------------------
-- DOWN (rollback) — uncomment to drop:
--
-- DROP INDEX IF EXISTS idx_matrix_backfill_done;
-- DROP TABLE IF EXISTS matrix_backfill_state;
