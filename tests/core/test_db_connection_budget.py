"""Connection recovery must respect its budget without touching a database."""
import asyncio

import pytest

from src.db import connection


@pytest.mark.asyncio
async def test_zero_retry_budget_makes_one_attempt(monkeypatch):
    attempts = 0

    async def fail(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError('synthetic unavailable')

    monkeypatch.setenv('DB_CONNECT_RETRY_TIMEOUT_SECONDS', '0')
    monkeypatch.setattr(connection.asyncpg, 'create_pool', fail)
    with pytest.raises(ConnectionRefusedError):
        await connection._create_pool_with_retry({})
    assert attempts == 1


@pytest.mark.asyncio
async def test_stalled_pool_initialization_ends_within_total_budget(monkeypatch):
    cancelled = asyncio.Event()

    async def stall(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setenv('DB_CONNECT_RETRY_TIMEOUT_SECONDS', '0.05')
    monkeypatch.setattr(connection.asyncpg, 'create_pool', stall)
    task = asyncio.create_task(connection._create_pool_with_retry({}))
    done, _ = await asyncio.wait({task}, timeout=0.5)
    if task not in done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        pytest.fail('Pool initialization ignored the complete retry budget')
    with pytest.raises(TimeoutError):
        task.result()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_first_retry_delay_respects_configured_maximum(monkeypatch):
    delays = []
    attempts = 0
    result = object()

    async def connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionRefusedError('synthetic unavailable')
        return result

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setenv('DB_CONNECT_RETRY_TIMEOUT_SECONDS', '10')
    monkeypatch.setenv('DB_CONNECT_RETRY_INITIAL_SECONDS', '5')
    monkeypatch.setenv('DB_CONNECT_RETRY_MAX_SECONDS', '0.1')
    monkeypatch.setattr(connection.asyncpg, 'create_pool', connect)
    monkeypatch.setattr(connection.asyncio, 'sleep', sleep)
    assert await connection._create_pool_with_retry({}) is result
    assert delays == [0.1]


@pytest.mark.asyncio
async def test_failed_pool_initialization_terminates_owned_pool(monkeypatch):
    pools = []

    async def fail(pool):
        pools.append(pool)
        raise ValueError('synthetic invalid setup')

    monkeypatch.setattr(connection.asyncpg.Pool, '_initialize', fail)
    with pytest.raises(ValueError, match='synthetic invalid setup'):
        await connection._create_pool_with_retry({'min_size': 1, 'max_size': 1})
    assert len(pools) == 1
    assert pools[0]._closed


@pytest.mark.parametrize('value', ['nan', 'inf', '-inf', 'not-a-number'])
def test_invalid_budget_uses_finite_default(monkeypatch, value):
    monkeypatch.setenv('DB_CONNECT_RETRY_TIMEOUT_SECONDS', value)
    assert connection._env_float('DB_CONNECT_RETRY_TIMEOUT_SECONDS', 180.0) == 180.0


@pytest.mark.asyncio
async def test_backoff_cannot_start_an_attempt_after_deadline(monkeypatch):
    attempts = 0

    async def fail(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError('synthetic unavailable')

    monkeypatch.setenv('DB_CONNECT_RETRY_TIMEOUT_SECONDS', '0.05')
    monkeypatch.setenv('DB_CONNECT_RETRY_INITIAL_SECONDS', '5')
    monkeypatch.setattr(connection.asyncpg, 'create_pool', fail)
    with pytest.raises((TimeoutError, ConnectionRefusedError)):
        await connection._create_pool_with_retry({})
    assert attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('budget', ['0', '30'])
async def test_caller_cancellation_closes_pool_and_allows_later_startup(monkeypatch, budget):
    started = asyncio.Event()
    pools = []

    async def initialize(pool):
        pools.append(pool)
        if len(pools) == 1:
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setenv('DB_CONNECT_RETRY_TIMEOUT_SECONDS', budget)
    monkeypatch.setattr(connection, '_pool', None)
    monkeypatch.setattr(connection, '_pool_lock', asyncio.Lock())
    monkeypatch.setattr(connection.asyncpg.Pool, '_initialize', initialize)
    task = asyncio.create_task(connection.get_pool())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pools[0]._closed
    assert connection._pool is None
    assert not connection._pool_lock.locked()
    ready = await connection.get_pool()
    assert ready is pools[1]
    ready.terminate()


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_successful_pool(monkeypatch):
    created = []

    async def initialize(pool):
        created.append(pool)
        await asyncio.sleep(0)

    monkeypatch.setattr(connection, '_pool', None)
    monkeypatch.setattr(connection, '_pool_lock', asyncio.Lock())
    monkeypatch.setattr(connection.asyncpg.Pool, '_initialize', initialize)
    pools = await asyncio.gather(*(connection.get_pool() for _ in range(20)))
    assert len(created) == 1
    assert all(pool is created[0] for pool in pools)
    created[0].terminate()


@pytest.mark.asyncio
async def test_real_driver_cancels_stalled_loopback_handshake(monkeypatch):
    """Exercise actual asyncpg sockets, with no PostgreSQL or external service."""
    accepted = asyncio.Event()
    closed = asyncio.Event()
    writers = []
    handlers = []

    async def blackhole(reader, writer):
        handlers.append(asyncio.current_task())
        writers.append(writer)
        accepted.set()
        try:
            while await reader.read(1024):
                pass
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    server = await asyncio.start_server(blackhole, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv('DATABASE_URL', f'postgresql://fixture:fixture@127.0.0.1:{port}/fixture')
    monkeypatch.setenv('DB_CONNECT_RETRY_TIMEOUT_SECONDS', '0.15')
    try:
        with pytest.raises(TimeoutError):
            await connection._create_pool_with_retry({'min_size': 1, 'max_size': 1, 'ssl': False})
        assert accepted.is_set()
        await asyncio.wait_for(closed.wait(), timeout=1)
    finally:
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()
        await asyncio.gather(*handlers, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_parallel_warmup_cancels_siblings_and_closes_ready_connections(monkeypatch):
    """Use the real asyncpg pool with an inert connection transport seam."""
    pending = []
    connections = []
    attempts = 0

    class InertConnection(connection.asyncpg.Connection):
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        def terminate(self):
            self.closed = True

        def __del__(self):
            pass

    async def connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            ready = InertConnection()
            connections.append(ready)
            return ready
        if attempts == 2:
            raise ConnectionRefusedError('synthetic second connection failure')
        pending.append(asyncio.current_task())
        await asyncio.Event().wait()

    monkeypatch.setenv('DB_CONNECT_RETRY_TIMEOUT_SECONDS', '0')
    try:
        with pytest.raises(ConnectionRefusedError):
            await connection._create_pool_with_retry({'min_size': 3, 'max_size': 3, 'connect': connect})
        assert pending and all(task.done() for task in pending)
        assert connections and all(conn.closed for conn in connections)
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
