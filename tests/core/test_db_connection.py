import pytest


@pytest.mark.asyncio
async def test_get_pool_retries_transient_startup_failure(monkeypatch):
    import src.db.connection as connection

    calls = 0

    class FakePool:
        pass

    async def fake_create_pool(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionRefusedError("database system is starting up")
        return FakePool()

    async def fake_sleep(_seconds):
        return None

    connection._pool = None
    monkeypatch.setenv("DATABASE_URL", "postgresql://collector:test@postgres:5432/unifiedcollector")
    monkeypatch.setenv("DB_CONNECT_RETRY_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("DB_CONNECT_RETRY_INITIAL_SECONDS", "0.1")
    monkeypatch.setenv("DB_CONNECT_RETRY_MAX_SECONDS", "0.1")
    monkeypatch.setattr(connection.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(connection.asyncio, "sleep", fake_sleep)

    try:
        pool = await connection.get_pool()
        assert isinstance(pool, FakePool)
        assert calls == 2
    finally:
        connection._pool = None


@pytest.mark.asyncio
async def test_get_pool_does_not_retry_non_connection_errors(monkeypatch):
    import src.db.connection as connection

    calls = 0

    async def fake_create_pool(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise ValueError("bad configuration")

    connection._pool = None
    monkeypatch.setenv("DATABASE_URL", "postgresql://collector:test@postgres:5432/unifiedcollector")
    monkeypatch.setattr(connection.asyncpg, "create_pool", fake_create_pool)

    try:
        with pytest.raises(ValueError, match="bad configuration"):
            await connection.get_pool()
        assert calls == 1
    finally:
        connection._pool = None
