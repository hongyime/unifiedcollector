-- P2-4: per-source health tracking + permanent-death alerting.
-- The watchdog previously gave up on a crash-looping source with only a log
-- line (logger.error "exceeded max restarts, giving up"), then went dark — no
-- persisted state, nothing queryable, no alert. This table records per-source
-- liveness and a 'dead' flag the metrics endpoint and operators can see.

CREATE TABLE IF NOT EXISTS source_health (
    source           TEXT PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'running',   -- running | dead | degraded
    last_success_at  TIMESTAMPTZ,
    last_error       TEXT,
    crash_count      INTEGER NOT NULL DEFAULT 0,
    died_at          TIMESTAMPTZ,                        -- set when status -> dead
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_source_health_status ON source_health (status);
