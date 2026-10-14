import asyncio
from unittest.mock import AsyncMock

import pytest

from src.worker import WorkerService


@pytest.mark.asyncio
async def test_release_source_mutex_unlocks_reentrant_holds_before_pool_release():
    worker = WorkerService.__new__(WorkerService)
    connection = AsyncMock()
    connection.fetchval = AsyncMock(side_effect=[True, True, False])
    worker.pool = AsyncMock()

    await worker._release_source_mutex((connection, "collector_mutex:search_engine"))

    assert connection.fetchval.await_count == 3
    worker.pool.release.assert_awaited_once_with(connection)


@pytest.mark.asyncio
async def test_release_source_mutex_finishes_unlock_after_cancellation():
    worker = WorkerService.__new__(WorkerService)
    connection = object()
    worker.pool = AsyncMock()
    started = asyncio.Event()
    finish_unlock = asyncio.Event()
    finished = asyncio.Event()

    async def unlock(_connection, _key):
        started.set()
        await finish_unlock.wait()
        finished.set()

    worker._unlock_source_mutex_connection = unlock
    release_task = asyncio.create_task(
        worker._release_source_mutex((connection, "collector_mutex:search_engine"))
    )
    await started.wait()
    release_task.cancel()
    finish_unlock.set()

    with pytest.raises(asyncio.CancelledError):
        await release_task

    assert finished.is_set()
    worker.pool.release.assert_awaited_once_with(connection)
