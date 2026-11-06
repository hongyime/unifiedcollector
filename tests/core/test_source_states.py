"""source_controls writer (Fudan taxonomy) + its migration's idempotence.

No database is available in CI for this suite, so the writer is verified
through the SQL it emits (upsert + failure-counter semantics) and the
migration is verified structurally: every statement must be IF NOT EXISTS
guarded, which is what makes applying it twice a no-op.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.source_states import SourceState, clear_automatic_source_state, provider_backoff_seconds, provider_failure, record_provider_block, set_source_state
from src.db import migrate

_MIGRATION_NAME = "20261029_add_source_controls.sql"
_MIGRATION = migrate.MIGRATIONS_DIR / _MIGRATION_NAME
_ROUTER = (
    Path(__file__).resolve().parents[2]
    / "src" / "core" / "source_controls.py"
)


class _FakeConn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.raise_exc: Exception | None = None

    async def execute(self, sql, *args):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append((sql, args))


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return None


@pytest.mark.asyncio
async def test_set_source_state_upserts_and_increments_error_states():
    conn = _FakeConn()
    await set_source_state(
        conn,
        "instagram",
        SourceState.RATE_LIMITED,
        stop_reason="HTTP 429",
        blocked_until=datetime(2026, 10, 29, tzinfo=timezone.utc),
    )

    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert "ON CONFLICT (source_name) DO UPDATE" in sql
    assert "source_controls.consecutive_failures + 1" in sql
    assert "WHEN EXCLUDED.state = 'HEALTHY' THEN 0" in sql
    assert args[0] == "instagram"
    assert args[1] == "RATE_LIMITED"
    assert args[4] == "HTTP 429"
    assert args[5] is True


@pytest.mark.asyncio
async def test_set_source_state_healthy_resets_counter_and_clears_block():
    conn = _FakeConn()
    await set_source_state(conn, "github", SourceState.HEALTHY)

    sql, args = conn.calls[0]
    assert args[1] == "HEALTHY"
    assert args[5] is False
    assert "WHEN EXCLUDED.state = 'HEALTHY' THEN NULL" in sql


@pytest.mark.asyncio
async def test_set_source_state_leaves_counter_alone_for_paused():
    conn = _FakeConn()
    await set_source_state(conn, "tiktok", SourceState.PAUSED)
    _, args = conn.calls[0]
    assert args[5] is False
    assert "ELSE source_controls.consecutive_failures END" in conn.calls[0][0]


@pytest.mark.asyncio
async def test_set_source_state_swallows_db_errors(caplog):
    conn = _FakeConn()
    conn.raise_exc = RuntimeError("boom")
    with caplog.at_level("ERROR", logger="src.core.source_states"):
        await set_source_state(conn, "tiktok", SourceState.SOURCE_DEAD)
    assert any("set_source_state failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_set_source_state_does_not_re_enable_an_operator_disabled_source():
    conn = _FakeConn()
    await set_source_state(conn, "instagram", SourceState.RATE_LIMITED, enabled=None)

    sql, args = conn.calls[0]
    flat = " ".join(sql.split())
    assert args[2] is None
    assert "COALESCE(EXCLUDED.enabled" not in flat
    assert (
        "CASE WHEN $3::boolean IS NULL THEN source_controls.enabled "
        "ELSE $3::boolean END" in flat
    )


@pytest.mark.asyncio
async def test_set_source_state_sets_enabled_when_explicitly_passed():
    conn = _FakeConn()
    await set_source_state(conn, "instagram", SourceState.PAUSED, enabled=True)

    sql, args = conn.calls[0]
    flat = " ".join(sql.split())
    assert args[2] is True
    assert (
        "CASE WHEN $3::boolean IS NULL THEN source_controls.enabled "
        "ELSE $3::boolean END" in flat
    )


def test_provider_backoff_honors_retry_after_and_caps_exponential():
    assert provider_backoff_seconds("45", 99) == 45
    assert provider_backoff_seconds(None, 1) == 30
    assert provider_backoff_seconds(None, 99) == 21600


def test_provider_failure_extracts_http_status_and_retry_after():
    response = type("Response", (), {"status_code": 429, "headers": {"Retry-After": "45"}})()
    exc = type("ProviderError", (Exception,), {"response": response})()
    assert provider_failure(exc) == (429, "45")


@pytest.mark.asyncio
async def test_record_provider_block_persists_retry_after_state():
    conn = _FakeConn()
    response = type("Response", (), {"status_code": 429, "headers": {"Retry-After": "45"}})()
    exc = type("ProviderError", (Exception,), {"response": response})()

    assert await record_provider_block(_FakePool(conn), "snapchat", exc, failures=3) == 45
    assert conn.calls[0][1][0:2] == ("snapchat", "RATE_LIMITED")
    assert conn.calls[0][1][4] == "HTTP 429; retry in 45s"


@pytest.mark.asyncio
async def test_recovery_never_overrides_manual_pause():
    conn = _FakeConn()
    await clear_automatic_source_state(conn, "tiktok")
    assert "state <> 'PAUSED'" in conn.calls[0][0]


def _executable_statements() -> list[str]:
    body = "\n".join(
        line for line in _MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    )
    return [stmt.strip() for stmt in body.split(";") if stmt.strip()]


def test_migration_uses_a_new_filename_and_is_picked_up():
    assert _MIGRATION.exists()
    assert _MIGRATION_NAME not in migrate.SKIP
    assert not (migrate.MIGRATIONS_DIR / "20261012_add_source_controls.sql").exists()
    ordered = [p.name for p in migrate._ordered_migrations(migrate.MIGRATIONS_DIR)]
    assert _MIGRATION_NAME in ordered


def test_migration_statements_are_all_re_runnable():
    statements = _executable_statements()
    assert statements
    for stmt in statements:
        assert stmt.startswith(
            (
                "CREATE TABLE IF NOT EXISTS",
                "ALTER TABLE",
                "CREATE INDEX IF NOT EXISTS",
                "CREATE UNIQUE INDEX IF NOT EXISTS",
            )
        ), stmt
        if stmt.startswith("ALTER TABLE"):
            assert stmt.count("ADD COLUMN IF NOT EXISTS") == 9, stmt


def test_migration_guarantees_the_conflict_target_is_unique():
    statements = _executable_statements()
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_source_controls_source_name " \
           "ON source_controls(source_name)" in statements


def test_migration_has_no_comment_on():
    assert all("COMMENT ON" not in stmt for stmt in _executable_statements())


def test_migration_defines_every_column_the_route_selects():
    body = _ROUTER.read_text(encoding="utf-8")
    select = body.rsplit("SELECT", 1)[1].split("FROM source_controls", 1)[0]
    columns = [c.strip() for c in select.replace("\n", " ").split(",") if c.strip()]
    assert len(columns) == 9
    migration = _MIGRATION.read_text(encoding="utf-8")
    for column in columns:
        assert column in migration, column
