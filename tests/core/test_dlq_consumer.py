import asyncio

import pytest

from src.core.dlq_consumer import DLQConsumer


class _FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.fetch_calls = []

    async def fetch(self, query, *args, **kwargs):
        self.fetch_calls.append((query, args, kwargs))
        return self.rows


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, rows):
        self.conn = _FakeConnection(rows)
        self.execute_calls = []

    def acquire(self):
        return _AcquireContext(self.conn)

    async def execute(self, query, *args, **kwargs):
        self.execute_calls.append((query, args, kwargs))
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_claim_batch_uses_single_atomic_update_with_timeout():
    row = {
        "id": 1,
        "source": "telegram",
        "entity_id": "chat",
        "content_id": "media",
        "error_message": "retry",
        "retry_count": 9,
        "created_at": None,
        "next_retry_at": None,
        "last_attempt_at": None,
        "status": "in_progress",
    }
    pool = _FakePool([row])
    consumer = DLQConsumer(pool, db_timeout_seconds=7)
    consumer.register("telegram", lambda _row: None)

    claimed = await consumer._claim_batch()

    assert claimed == [row]
    query, args, kwargs = pool.conn.fetch_calls[0]
    assert "WITH claimed AS" in query
    assert "UPDATE dead_letter_queue AS dlq" in query
    assert "RETURNING dlq.id" in query
    assert "FOR UPDATE SKIP LOCKED" in query
    assert "source = ANY($2::text[])" in query
    assert args == (16, ["telegram"])
    assert kwargs == {"timeout": 7}


@pytest.mark.asyncio
async def test_claim_batch_without_handlers_does_not_touch_db():
    pool = _FakePool([])
    consumer = DLQConsumer(pool)

    assert await consumer._claim_batch() == []
    assert pool.conn.fetch_calls == []


@pytest.mark.asyncio
async def test_handler_timeout_reschedules_row():
    row = {
        "id": 42,
        "source": "telegram",
        "entity_id": "chat",
        "content_id": "media",
        "error_message": "retry",
        "retry_count": 0,
        "created_at": None,
        "next_retry_at": None,
        "last_attempt_at": None,
        "status": "in_progress",
    }
    pool = _FakePool([])
    consumer = DLQConsumer(
        pool,
        handler_timeout_seconds=0.01,
        base_backoff_seconds=0.01,
        max_backoff_seconds=0.01,
    )

    async def stuck_handler(_row):
        await asyncio.sleep(10)

    consumer.register("telegram", stuck_handler)
    await consumer._process_row(row)

    assert consumer.stats["retried"] == 1
    query, args, _kwargs = pool.execute_calls[0]
    assert "SET status = 'pending'" in query
    assert args[0] == 42
    assert args[1] == 1
    assert "TimeoutError" in args[2]


def test_handler_timeout_must_be_positive():
    with pytest.raises(ValueError, match="handler_timeout_seconds"):
        DLQConsumer(_FakePool([]), handler_timeout_seconds=0)
