"""Fixtures for recovery tests that need a disposable PostgreSQL 16 database.

Import these fixtures from future recovery tests (tasks 7-30):

    async def test_restore(pg_conn):
        assert await pg_conn.fetchval("SELECT 1") == 1

* ``disposable_pg`` -- owns (or wraps) the target; teardown always runs.
* ``pg_dsn``       -- marker-free DSN accepted by ``asyncpg``.
* ``pg_conn``      -- open ``asyncpg.Connection``, closed before teardown.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

from tests.recovery.pg_harness import (
    DisposablePostgres,
    HarnessUnavailableError,
    assert_environment_safe,
    resolve_external_dsn,
)

SKIP_WITHOUT_DOCKER_ENV = "UC_RECOVERY_TEST_ALLOW_SKIP_NO_DOCKER"


@pytest.fixture(scope="session")
def disposable_pg():
    """Start/wrap one disposable database and guarantee cleanup in ``finally``."""
    external_dsn = resolve_external_dsn()
    # Refuse a production DATABASE_URL before creating any Docker resource.
    assert_environment_safe(external_dsn or "")
    if external_dsn is not None:
        yield DisposablePostgres.external(external_dsn)
        return

    try:
        harness = DisposablePostgres.start()
    except HarnessUnavailableError as exc:
        if os.environ.get(SKIP_WITHOUT_DOCKER_ENV) == "1":
            pytest.skip(f"{SKIP_WITHOUT_DOCKER_ENV}=1 explicit override: {exc}")
        pytest.fail(str(exc), pytrace=False)

    try:
        assert_environment_safe(harness.dsn)
        yield harness
    finally:
        harness.stop()


@pytest.fixture(scope="session")
def pg_dsn(disposable_pg: DisposablePostgres) -> str:
    """Fixture-owned DSN with the guard-only marker removed."""
    return disposable_pg.driver_dsn


@pytest_asyncio.fixture
async def pg_conn(disposable_pg: DisposablePostgres):
    """Open a fresh asyncpg connection to the disposable database.

    ``pg_isready`` can briefly succeed against the image's temporary initdb
    server, which then restarts before the real server accepts TCP.  Retry a
    few times so that benign restart resets do not fail an otherwise healthy
    harness.
    """
    import asyncio

    import asyncpg

    last_error: Exception | None = None
    connection = None
    for _ in range(20):
        try:
            connection = await asyncpg.connect(disposable_pg.driver_dsn)
            break
        except Exception as exc:  # noqa: BLE001 - retry transient startup resets
            last_error = exc
            await asyncio.sleep(0.25)
    if connection is None:
        assert last_error is not None
        raise last_error
    try:
        yield connection
    finally:
        await connection.close()
