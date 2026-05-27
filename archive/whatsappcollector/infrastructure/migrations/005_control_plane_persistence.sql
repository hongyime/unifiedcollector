-- Migration 005 — Control-plane persistence foundation
-- Adds persistent control-plane tables for dashboard-only operations:
-- - bootstrap state
-- - current desired config values
-- - encrypted secrets store
-- - config version history
-- - immutable audit log
-- - restart orchestration queue and events
--
-- Idempotent: all CREATE statements use IF NOT EXISTS guards.
-- Run AFTER 004_link_discovery_session_control.sql.

CREATE SCHEMA IF NOT EXISTS collector;

-- -------------------------------------------------------------------------
-- Bootstrap lifecycle state (single-row table)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collector.control_bootstrap_state (
    singleton_id        SMALLINT PRIMARY KEY DEFAULT 1,
    state               VARCHAR(32) NOT NULL DEFAULT 'uninitialized',
    wizard_version      VARCHAR(32),
    generated_defaults  JSONB NOT NULL DEFAULT '{}'::jsonb,
    initialized_by      VARCHAR(255),
    initialized_at      TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_control_bootstrap_singleton CHECK (singleton_id = 1),
    CONSTRAINT chk_control_bootstrap_state
        CHECK (state IN ('uninitialized', 'wizard_in_progress', 'initialized'))
);

INSERT INTO collector.control_bootstrap_state (singleton_id)
VALUES (1)
ON CONFLICT (singleton_id) DO NOTHING;

-- -------------------------------------------------------------------------
-- Current desired configuration values (source-of-truth for non-secret keys)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collector.control_config_values (
    service_name        VARCHAR(100) NOT NULL,
    config_key          VARCHAR(200) NOT NULL,
    value_json          JSONB NOT NULL DEFAULT 'null'::jsonb,
    scope               VARCHAR(20) NOT NULL DEFAULT 'runtime',
    is_secret           BOOLEAN NOT NULL DEFAULT FALSE,
    requires_restart    BOOLEAN NOT NULL DEFAULT FALSE,
    version             BIGINT NOT NULL DEFAULT 1,
    updated_by          VARCHAR(255),
    update_reason       TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (service_name, config_key),
    CONSTRAINT chk_control_config_scope CHECK (scope IN ('runtime', 'restart', 'bootstrap'))
);

CREATE INDEX IF NOT EXISTS idx_control_config_values_service
    ON collector.control_config_values(service_name);
CREATE INDEX IF NOT EXISTS idx_control_config_values_updated_at
    ON collector.control_config_values(updated_at DESC);

-- -------------------------------------------------------------------------
-- Encrypted secret storage (ciphertext-only at rest)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collector.control_secret_values (
    service_name        VARCHAR(100) NOT NULL,
    secret_key          VARCHAR(200) NOT NULL,
    ciphertext          BYTEA NOT NULL,
    nonce               BYTEA,
    auth_tag            BYTEA,
    encryption_key_id   VARCHAR(128) NOT NULL DEFAULT 'local-kek-v1',
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by          VARCHAR(255),
    update_reason       TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (service_name, secret_key)
);

CREATE INDEX IF NOT EXISTS idx_control_secret_values_updated_at
    ON collector.control_secret_values(updated_at DESC);

-- -------------------------------------------------------------------------
-- Config version history / snapshots for rollback and diffs
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collector.control_config_versions (
    version_id          BIGSERIAL PRIMARY KEY,
    service_name        VARCHAR(100) NOT NULL,
    config_key          VARCHAR(200) NOT NULL,
    old_value_json      JSONB,
    new_value_json      JSONB,
    changed_by          VARCHAR(255),
    change_reason       TEXT,
    change_source       VARCHAR(64) NOT NULL DEFAULT 'dashboard',
    request_id          VARCHAR(128),
    is_secret           BOOLEAN NOT NULL DEFAULT FALSE,
    requires_restart    BOOLEAN NOT NULL DEFAULT FALSE,
    changed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_control_config_versions_lookup
    ON collector.control_config_versions(service_name, config_key, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_control_config_versions_request
    ON collector.control_config_versions(request_id)
    WHERE request_id IS NOT NULL;

-- -------------------------------------------------------------------------
-- Immutable audit stream for all control-plane mutations
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collector.control_change_log (
    event_id            BIGSERIAL PRIMARY KEY,
    event_type          VARCHAR(64) NOT NULL,
    service_name        VARCHAR(100),
    config_key          VARCHAR(200),
    actor_id            VARCHAR(255),
    actor_role          VARCHAR(64),
    event_source        VARCHAR(64) NOT NULL DEFAULT 'dashboard',
    request_id          VARCHAR(128),
    old_value_masked    TEXT,
    new_value_masked    TEXT,
    reason              TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_control_change_log_created_at
    ON collector.control_change_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_control_change_log_lookup
    ON collector.control_change_log(service_name, config_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_control_change_log_request
    ON collector.control_change_log(request_id)
    WHERE request_id IS NOT NULL;

CREATE OR REPLACE FUNCTION collector.prevent_control_change_log_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'collector.control_change_log is immutable';
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'collector'
          AND c.relname = 'control_change_log'
          AND t.tgname = 'trg_control_change_log_immutable'
    ) THEN
        CREATE TRIGGER trg_control_change_log_immutable
        BEFORE UPDATE OR DELETE ON collector.control_change_log
        FOR EACH ROW
        EXECUTE FUNCTION collector.prevent_control_change_log_mutation();
    END IF;
END
$$;

-- -------------------------------------------------------------------------
-- Restart orchestration queue for restart-required config changes
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collector.control_restart_jobs (
    job_id               BIGSERIAL PRIMARY KEY,
    service_name         VARCHAR(100) NOT NULL,
    status               VARCHAR(32) NOT NULL DEFAULT 'pending',
    requested_by         VARCHAR(255),
    request_reason       TEXT,
    request_id           VARCHAR(128),
    config_version_id    BIGINT REFERENCES collector.control_config_versions(version_id) ON DELETE SET NULL,
    health_before        JSONB NOT NULL DEFAULT '{}'::jsonb,
    health_after         JSONB NOT NULL DEFAULT '{}'::jsonb,
    failure_reason       TEXT,
    requested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at           TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    CONSTRAINT chk_control_restart_status
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_control_restart_jobs_status
    ON collector.control_restart_jobs(status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_control_restart_jobs_service
    ON collector.control_restart_jobs(service_name, status, requested_at DESC);

CREATE TABLE IF NOT EXISTS collector.control_restart_events (
    event_id             BIGSERIAL PRIMARY KEY,
    job_id               BIGINT NOT NULL REFERENCES collector.control_restart_jobs(job_id) ON DELETE CASCADE,
    event_type           VARCHAR(64) NOT NULL,
    event_payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_control_restart_events_job
    ON collector.control_restart_events(job_id, created_at DESC);
