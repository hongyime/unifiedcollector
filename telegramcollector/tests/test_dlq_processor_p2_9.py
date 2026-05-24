"""
Tests for P2.9: DLQProcessor._remove_from_dlq() uses task ID (hash-based DLQ).

Validates: Requirements 2.18 - DLQProcessor._remove_from_dlq() removes entries
reliably by ID using Redis hash operations instead of exact JSON match.

Validates: Requirements 3.15 - Preservation: removal still works with consistent
JSON serialization (backward compatibility).
"""
import asyncio
import json
import pytest
from unittest.mock import MagicMock, AsyncMock, call
from shared.dlq import DLQProcessor, DLQEntry, ErrorType
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_redis_mock():
    """Returns a synchronous Redis mock (methods are called via run_in_executor)."""
    redis = MagicMock()
    redis.hset = MagicMock(return_value=1)
    redis.hdel = MagicMock(return_value=1)
    redis.hgetall = MagicMock(return_value={})
    return redis


def make_processor(redis=None):
    if redis is None:
        redis = make_redis_mock()
    return DLQProcessor(redis_client=redis)


def make_entry(task_data: dict, error_reason: str = "timeout") -> DLQEntry:
    return DLQEntry(
        task_data=task_data,
        error_reason=error_reason,
        error_type=ErrorType.TRANSIENT,
        failed_at=datetime.now(timezone.utc),
        retry_count=0,
    )


# ---------------------------------------------------------------------------
# _add_to_dlq tests
# ---------------------------------------------------------------------------

class TestAddToDlq:
    """Validates: Requirements 2.18 - DLQ uses hash with task ID keys."""

    @pytest.mark.asyncio
    async def test_add_assigns_task_id(self):
        """_add_to_dlq must assign a _task_id to the task data."""
        redis = make_redis_mock()
        proc = make_processor(redis)

        task_data = {"chat_id": 1, "message_id": 42}
        await proc._add_to_dlq(task_data, error_reason="timeout")

        assert "_task_id" in task_data
        assert len(task_data["_task_id"]) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_add_uses_hset(self):
        """_add_to_dlq must store the entry via hset (hash), not lpush (list)."""
        redis = make_redis_mock()
        proc = make_processor(redis)

        task_data = {"chat_id": 1}
        await proc._add_to_dlq(task_data, error_reason="network error")

        task_id = task_data["_task_id"]
        redis.hset.assert_called_once_with(proc.dlq_key, task_id, json.dumps(task_data))

    @pytest.mark.asyncio
    async def test_add_stores_failure_metadata(self):
        """_add_to_dlq must include _failure_reason and _failed_at in stored data."""
        redis = make_redis_mock()
        proc = make_processor(redis)

        task_data = {"chat_id": 5}
        await proc._add_to_dlq(task_data, error_reason="connection reset")

        stored_json = redis.hset.call_args[0][2]
        stored = json.loads(stored_json)
        assert stored["_failure_reason"] == "connection reset"
        assert "_failed_at" in stored

    @pytest.mark.asyncio
    async def test_add_two_tasks_get_different_ids(self):
        """Each call to _add_to_dlq must produce a unique task ID."""
        redis = make_redis_mock()
        proc = make_processor(redis)

        task1 = {"chat_id": 1}
        task2 = {"chat_id": 2}
        await proc._add_to_dlq(task1)
        await proc._add_to_dlq(task2)

        assert task1["_task_id"] != task2["_task_id"]


# ---------------------------------------------------------------------------
# _remove_from_dlq tests
# ---------------------------------------------------------------------------

class TestRemoveFromDlq:
    """Validates: Requirements 2.18 - removal works by ID regardless of dict ordering."""

    @pytest.mark.asyncio
    async def test_remove_calls_hdel_with_task_id(self):
        """_remove_from_dlq must call hdel with the task's _task_id."""
        redis = make_redis_mock()
        proc = make_processor(redis)

        task_data = {"chat_id": 1, "_task_id": "abc-123"}
        entry = make_entry(task_data)

        await proc._remove_from_dlq(entry)

        redis.hdel.assert_called_once_with(proc.dlq_key, "abc-123")

    @pytest.mark.asyncio
    async def test_remove_works_regardless_of_dict_ordering(self):
        """Removal must succeed even when dict keys are in different order than stored."""
        redis = make_redis_mock()
        proc = make_processor(redis)

        # Simulate: task was stored with one ordering, but entry has different ordering
        task_id = "fixed-task-id-999"
        task_data_original = {"b": 2, "a": 1, "_task_id": task_id}
        task_data_reordered = {"a": 1, "b": 2, "_task_id": task_id}

        entry = make_entry(task_data_reordered)
        await proc._remove_from_dlq(entry)

        # hdel is called with the task_id, not with JSON — so ordering is irrelevant
        redis.hdel.assert_called_once_with(proc.dlq_key, task_id)

    @pytest.mark.asyncio
    async def test_remove_missing_task_id_logs_warning_no_hdel(self):
        """If _task_id is absent, hdel must NOT be called (no silent corruption)."""
        redis = make_redis_mock()
        proc = make_processor(redis)

        task_data = {"chat_id": 1}  # no _task_id
        entry = make_entry(task_data)

        await proc._remove_from_dlq(entry)

        redis.hdel.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_after_add_leaves_no_entry(self):
        """After add then remove, the hash should have no entry for that task_id."""
        # Use a real in-memory dict to simulate Redis hash behaviour
        store: dict = {}

        redis = MagicMock()
        redis.hset = MagicMock(side_effect=lambda key, field, value: store.update({field: value}))
        redis.hdel = MagicMock(side_effect=lambda key, field: store.pop(field, None))
        redis.hgetall = MagicMock(return_value=store)

        proc = make_processor(redis)

        task_data = {"chat_id": 7, "message_id": 99}
        await proc._add_to_dlq(task_data, error_reason="timeout")

        task_id = task_data["_task_id"]
        assert task_id in store  # entry was added

        entry = make_entry(task_data)
        await proc._remove_from_dlq(entry)

        assert task_id not in store  # entry was removed


# ---------------------------------------------------------------------------
# get_all_entries tests (hash-based read)
# ---------------------------------------------------------------------------

class TestGetAllEntries:
    """Validates: Requirements 2.18 - DLQ reads from hash, not list."""

    @pytest.mark.asyncio
    async def test_get_all_entries_uses_hgetall(self):
        """get_all_entries must use hgetall, not lrange."""
        redis = make_redis_mock()
        redis.hgetall = MagicMock(return_value={})
        proc = make_processor(redis)

        await proc.get_all_entries()

        redis.hgetall.assert_called_once_with(proc.dlq_key)

    @pytest.mark.asyncio
    async def test_get_all_entries_parses_stored_tasks(self):
        """get_all_entries must return DLQEntry objects for each hash field."""
        task_id = "task-001"
        task_json = json.dumps({
            "_task_id": task_id,
            "_failure_reason": "timeout",
            "_failed_at": datetime.now(timezone.utc).isoformat(),
            "_retry_count": 1,
            "chat_id": 10,
        })

        redis = make_redis_mock()
        redis.hgetall = MagicMock(return_value={task_id.encode(): task_json.encode()})
        proc = make_processor(redis)

        entries = await proc.get_all_entries()

        assert len(entries) == 1
        assert entries[0].task_data["_task_id"] == task_id
        assert entries[0].retry_count == 1

    @pytest.mark.asyncio
    async def test_get_all_entries_sets_task_id_from_hash_key(self):
        """If stored JSON lacks _task_id, it should be populated from the hash key."""
        task_id = "hash-key-id"
        task_json = json.dumps({
            "_failure_reason": "network",
            "_failed_at": datetime.now(timezone.utc).isoformat(),
            "_retry_count": 0,
        })

        redis = make_redis_mock()
        redis.hgetall = MagicMock(return_value={task_id.encode(): task_json.encode()})
        proc = make_processor(redis)

        entries = await proc.get_all_entries()

        assert entries[0].task_data["_task_id"] == task_id


# ---------------------------------------------------------------------------
# Round-trip test
# ---------------------------------------------------------------------------

class TestRoundTrip:
    """Validates: Requirements 3.15 - preservation: add/remove cycle works correctly."""

    @pytest.mark.asyncio
    async def test_add_remove_round_trip(self):
        """Full round-trip: add a task, retrieve it, remove it — store is empty after."""
        store: dict = {}

        redis = MagicMock()
        redis.hset = MagicMock(side_effect=lambda key, field, value: store.update({field: value}))
        redis.hdel = MagicMock(side_effect=lambda key, field: store.pop(field, None))
        redis.hgetall = MagicMock(return_value=store)

        proc = make_processor(redis)

        # Add
        task_data = {"chat_id": 3, "message_id": 77, "extra": "data"}
        await proc._add_to_dlq(task_data, error_reason="rate limit")
        assert len(store) == 1

        # Retrieve
        entries = await proc.get_all_entries()
        assert len(entries) == 1

        # Remove
        await proc._remove_from_dlq(entries[0])
        assert len(store) == 0
