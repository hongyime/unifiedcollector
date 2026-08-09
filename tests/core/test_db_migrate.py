from contextlib import asynccontextmanager

import pytest

from src.db import migrate


class _LockedConn:
    async def fetchval(self, *_args, **_kwargs):
        return False


class _LockedPool:
    @asynccontextmanager
    async def acquire(self):
        yield _LockedConn()


class _LockErrorConn:
    async def fetchval(self, *_args, **_kwargs):
        raise RuntimeError("db startup race")


class _LockErrorPool:
    @asynccontextmanager
    async def acquire(self):
        yield _LockErrorConn()


@pytest.mark.asyncio
async def test_apply_all_logs_advisory_lock_miss_as_info(caplog):
    with caplog.at_level("INFO", logger="src.db.migrate"):
        summary = await migrate.apply_all(_LockedPool())

    assert summary["deferred"] is True
    assert any(
        record.levelname == "INFO"
        and "another instance is currently migrating" in record.message
        for record in caplog.records
    )
    assert not any(
        record.levelname == "WARNING"
        and "another instance is currently migrating" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_apply_all_defers_when_advisory_lock_check_fails(caplog):
    with caplog.at_level("WARNING", logger="src.db.migrate"):
        summary = await migrate.apply_all(_LockErrorPool())

    assert summary["deferred"] is True
    assert any(
        record.levelname == "WARNING"
        and "advisory lock check failed" in record.message
        for record in caplog.records
    )
