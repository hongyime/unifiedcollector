from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.source_config import _sync_targets_to_db


def _pool():
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"target_id": "old"}])

    @asynccontextmanager
    async def _transaction():
        yield

    @asynccontextmanager
    async def _acquire():
        yield conn

    conn.transaction = _transaction
    pool = MagicMock()
    pool.acquire = _acquire
    pool.conn = conn
    return pool


@pytest.mark.asyncio
async def test_source_config_preserves_existing_targets_by_default(monkeypatch):
    monkeypatch.delenv("COLLECTOR_SOURCE_CONFIG_AUTHORITATIVE", raising=False)
    pool = _pool()

    removed = await _sync_targets_to_db(
        pool,
        "github",
        [{"target_id": "seed", "name": None, "priority": 5}],
    )

    assert removed == 0
    pool.conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_config_can_delete_missing_targets_in_authoritative_mode(monkeypatch):
    monkeypatch.setenv("COLLECTOR_SOURCE_CONFIG_AUTHORITATIVE", "true")
    pool = _pool()

    removed = await _sync_targets_to_db(
        pool,
        "github",
        [{"target_id": "seed", "name": None, "priority": 5}],
    )

    assert removed == 1
    assert "DELETE FROM collection_targets" in pool.conn.fetch.await_args.args[0]
