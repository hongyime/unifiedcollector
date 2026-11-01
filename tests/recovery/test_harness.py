"""Acceptance tests for the disposable PostgreSQL 16 recovery harness.

Run with::

    uv run pytest -q tests/recovery/test_harness.py

The tests never touch the production database, port 5433, or any
``unifiedcollector_*`` resource.  Every Docker object is named
``uc-recovery-test-<uuid>*`` and labelled ``uc.recovery.test=1``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.recovery.pg_harness import (
    DisposableDsnError,
    DisposablePostgres,
    IMAGE,
    assert_disposable_dsn,
    assert_environment_safe,
    disposable_dsn_reasons,
    is_disposable_dsn,
    list_test_resources,
    resolve_external_dsn,
)

HERE = Path(__file__).resolve().parent
GUARD_SCRIPT = HERE / "pg_harness.py"
PRODUCTION_DSN = "postgresql://collector:do-not-use@127.0.0.1:5433/unifiedcollector"
SYNTHETIC_DISPOSABLE_DSN = (
    "postgresql://uc_recovery_test:fixture-only@127.0.0.1:65432/"
    "uc_recovery_test_evidence?uc_recovery_test=evidence"
)


def run_guard(dsn: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD_SCRIPT), "--assert-disposable", dsn],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    "dsn",
    [
        PRODUCTION_DSN,
        "postgresql://collector:do-not-use@db.internal:5432/uc_recovery_test_x?uc_recovery_test=x",
        "postgresql://collector:do-not-use@0.0.0.0:5432/uc_recovery_test_x?uc_recovery_test=x",
        "postgresql://collector:do-not-use@127.0.0.1:5432/unifiedcollector_pgdata?uc_recovery_test=x",
        "postgresql://collector:do-not-use@127.0.0.1:5432/uc_recovery_test_x",
        "postgresql://collector:do-not-use@127.0.0.1:5432/uc_recovery_test_x"
        "?uc_recovery_test=x&host=production.example",
        "mysql://collector:do-not-use@127.0.0.1:3306/uc_recovery_test_x?uc_recovery_test=x",
    ],
)
def test_guard_rejects_production_and_unowned_dsns(dsn: str) -> None:
    assert not is_disposable_dsn(dsn), dsn
    with pytest.raises(DisposableDsnError):
        assert_disposable_dsn(dsn)
    completed = run_guard(dsn)
    assert completed.returncode == 2, completed
    assert "REJECTED" in completed.stderr


def test_guard_accepts_marked_loopback_disposable_dsn() -> None:
    assert_disposable_dsn(SYNTHETIC_DISPOSABLE_DSN)
    completed = run_guard(SYNTHETIC_DISPOSABLE_DSN)
    assert completed.returncode == 0, completed
    assert "ACCEPTED" in completed.stdout
    assert "fixture-only" not in completed.stdout


def test_guard_cli_accepts_harness_owned_dsn(disposable_pg: DisposablePostgres) -> None:
    completed = run_guard(disposable_pg.dsn)
    assert completed.returncode == 0, completed
    assert "ACCEPTED" in completed.stdout
    assert disposable_pg.password not in completed.stdout


def test_environment_refuses_production_database_url(
    monkeypatch: pytest.MonkeyPatch, disposable_pg: DisposablePostgres
) -> None:
    monkeypatch.setenv("DATABASE_URL", PRODUCTION_DSN)
    with pytest.raises(DisposableDsnError):
        assert_environment_safe(disposable_pg.dsn)

    monkeypatch.setenv("DATABASE_URL", SYNTHETIC_DISPOSABLE_DSN)
    with pytest.raises(DisposableDsnError):
        assert_environment_safe(disposable_pg.dsn)

    monkeypatch.setenv("DATABASE_URL", disposable_pg.dsn)
    assert_environment_safe(disposable_pg.dsn)


def test_external_dsn_requires_same_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UC_RECOVERY_TEST_DSN", PRODUCTION_DSN)
    with pytest.raises(DisposableDsnError):
        resolve_external_dsn()

    monkeypatch.setenv("UC_RECOVERY_TEST_DSN", SYNTHETIC_DISPOSABLE_DSN)
    assert resolve_external_dsn() == SYNTHETIC_DISPOSABLE_DSN


def test_harness_uses_loopback_ephemeral_port_and_pinned_image(
    disposable_pg: DisposablePostgres,
) -> None:
    parts = urlsplit(disposable_pg.dsn)
    assert parts.hostname in {"127.0.0.1", "localhost", "::1"}
    assert parts.port != 5433
    assert parts.port is not None
    assert disposable_pg.image == IMAGE
    if disposable_pg.owned:
        assert parts.hostname == "127.0.0.1"
        assert disposable_pg.image_id.startswith("sha256:")
        assert disposable_pg.container_name.startswith("uc-recovery-test-")
        assert disposable_pg.volume_name.startswith("uc-recovery-test-")
        assert disposable_pg.network_name.startswith("uc-recovery-test-")
        assert disposable_pg.database.startswith("uc_recovery_test_")
        assert "unifiedcollector" not in disposable_pg.database


async def test_server_is_postgresql_16(pg_conn) -> None:
    version = await pg_conn.fetchval("SELECT version()")
    assert version.startswith("PostgreSQL 16"), version


async def test_pgvector_extension_can_be_created(pg_conn) -> None:
    await pg_conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    extension = await pg_conn.fetchrow(
        "SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'"
    )
    assert extension is not None
    assert extension["extname"] == "vector"
    assert extension["extversion"]


async def test_schema_create_insert_select_roundtrip(pg_conn) -> None:
    await pg_conn.execute("DROP SCHEMA IF EXISTS recovery_probe CASCADE")
    await pg_conn.execute("CREATE SCHEMA recovery_probe")
    await pg_conn.execute(
        """
        CREATE TABLE recovery_probe.items (
            id BIGSERIAL PRIMARY KEY,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    inserted = await pg_conn.fetchrow(
        "INSERT INTO recovery_probe.items(payload) VALUES($1::jsonb) "
        "RETURNING id, payload, created_at",
        json.dumps({"source": "fixture", "restored": True}),
    )
    assert inserted["id"] == 1
    selected = await pg_conn.fetchrow(
        "SELECT payload FROM recovery_probe.items WHERE id = $1", inserted["id"]
    )
    assert json.loads(selected["payload"]) == {"source": "fixture", "restored": True}


async def test_cursor_upsert_update_read(pg_conn) -> None:
    await pg_conn.execute("DROP SCHEMA IF EXISTS recovery_probe CASCADE")
    await pg_conn.execute("CREATE SCHEMA recovery_probe")
    await pg_conn.execute(
        """
        CREATE TABLE recovery_probe.service_cursors (
            service VARCHAR(50) PRIMARY KEY,
            last_processed_id VARCHAR(100),
            last_processed_at TIMESTAMPTZ,
            status VARCHAR(20) NOT NULL DEFAULT 'idle'
        )
        """
    )
    await pg_conn.execute(
        """
        INSERT INTO recovery_probe.service_cursors(service, status)
        VALUES($1, 'idle')
        ON CONFLICT (service) DO UPDATE SET service = EXCLUDED.service
        """,
        "fixture_collector",
    )
    await pg_conn.execute(
        """
        UPDATE recovery_probe.service_cursors
        SET last_processed_id = $2, last_processed_at = NOW(), status = 'running'
        WHERE service = $1
        """,
        "fixture_collector",
        "cursor-0001",
    )
    cursor = await pg_conn.fetchrow(
        "SELECT last_processed_id, last_processed_at, status "
        "FROM recovery_probe.service_cursors WHERE service = $1",
        "fixture_collector",
    )
    assert cursor["last_processed_id"] == "cursor-0001"
    assert cursor["last_processed_at"] is not None
    assert cursor["status"] == "running"


def test_zz_teardown_removes_disposable_resources() -> None:
    """Prove teardown on an owned harness without stopping the session fixture."""
    harness = DisposablePostgres.start()
    try:
        before = list_test_resources(harness.run_id)
        assert harness.container_name in before["containers"]
        assert harness.volume_name in before["volumes"]
        assert harness.network_name in before["networks"]

        harness.stop()
        harness.stop()  # idempotent

        after = list_test_resources(harness.run_id)
        assert after == {"containers": [], "volumes": [], "networks": []}, after
    finally:
        harness.stop()


def test_harness_module_documents_disposable_rules() -> None:
    reasons = disposable_dsn_reasons(PRODUCTION_DSN)
    assert any("5433" in reason for reason in reasons)
    assert any("unifiedcollector" in reason for reason in reasons)
