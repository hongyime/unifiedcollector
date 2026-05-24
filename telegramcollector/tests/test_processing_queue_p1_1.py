"""
Tests for ProcessingQueue.get_queue_size() - Task P1.1

Validates: Bug F-003 fix - get_queue_size() method exists and returns correct queue depth.

Fix Checking Property (F-003):
  FOR ALL X WHERE isBugCondition(X) DO
    result <- get_queue_size'(X)
    ASSERT result IS INTEGER AND result >= 0
  END FOR

Preservation Checking Property (F-003):
  FOR ALL X WHERE NOT isBugCondition(X) DO
    ASSERT get_queue_size(X) == get_queue_size'(X)
  END FOR
"""

import asyncio
import queue
import unittest
from unittest.mock import MagicMock, patch, PropertyMock


def _make_queue_no_redis(high_watermark=100, low_watermark=20):
    """Create a ProcessingQueue with Redis disabled (fallback mode)."""
    with patch("shared.processing_queue.redis") as mock_redis_module:
        mock_redis_module.Redis.side_effect = Exception("Redis unavailable")
        from shared.processing_queue import ProcessingQueue
        pq = ProcessingQueue.__new__(ProcessingQueue)
        # Manually set the minimal attributes needed
        pq.redis_available = False
        pq.redis_client = None
        pq.fallback_queue = asyncio.Queue()
        pq.queue_key = "processing_queue:tasks"
        return pq


def _make_queue_with_redis(llen_return_value: int):
    """Create a ProcessingQueue with a mocked Redis client."""
    from shared.processing_queue import ProcessingQueue
    pq = ProcessingQueue.__new__(ProcessingQueue)
    mock_redis = MagicMock()
    mock_redis.llen.return_value = llen_return_value
    pq.redis_available = True
    pq.redis_client = mock_redis
    pq.fallback_queue = asyncio.Queue()
    pq.queue_key = "processing_queue:tasks"
    return pq


class TestGetQueueSizeMethodExists(unittest.TestCase):
    """Fix Checking: method must exist on ProcessingQueue."""

    def test_method_exists(self):
        """get_queue_size() must be present on ProcessingQueue class."""
        from shared.processing_queue import ProcessingQueue
        self.assertTrue(
            hasattr(ProcessingQueue, "get_queue_size"),
            "ProcessingQueue must have a get_queue_size() method"
        )

    def test_method_is_callable(self):
        from shared.processing_queue import ProcessingQueue
        self.assertTrue(callable(getattr(ProcessingQueue, "get_queue_size", None)))


class TestGetQueueSizeRedisAvailable(unittest.TestCase):
    """Fix Checking + Preservation: Redis path returns correct integer."""

    def test_returns_integer(self):
        """Validates: Requirements 2.3 - returns integer without AttributeError."""
        pq = _make_queue_with_redis(llen_return_value=5)
        result = pq.get_queue_size()
        self.assertIsInstance(result, int)

    def test_empty_queue_returns_zero(self):
        """Returns 0 when Redis reports empty queue."""
        pq = _make_queue_with_redis(llen_return_value=0)
        self.assertEqual(pq.get_queue_size(), 0)

    def test_populated_queue_returns_accurate_count(self):
        """Returns accurate count matching Redis llen."""
        for count in [1, 10, 50, 100, 999]:
            with self.subTest(count=count):
                pq = _make_queue_with_redis(llen_return_value=count)
                self.assertEqual(pq.get_queue_size(), count)

    def test_result_is_non_negative(self):
        """Queue size must always be >= 0."""
        pq = _make_queue_with_redis(llen_return_value=0)
        self.assertGreaterEqual(pq.get_queue_size(), 0)

    def test_uses_correct_queue_key(self):
        """Redis llen is called with the correct queue key."""
        pq = _make_queue_with_redis(llen_return_value=3)
        pq.get_queue_size()
        pq.redis_client.llen.assert_called_once_with("processing_queue:tasks")

    def test_redis_exception_falls_back_to_in_memory(self):
        """If Redis raises, falls back to fallback_queue.qsize()."""
        pq = _make_queue_with_redis(llen_return_value=0)
        pq.redis_client.llen.side_effect = Exception("Redis error")
        # fallback_queue is empty
        result = pq.get_queue_size()
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 0)


class TestGetQueueSizeFallback(unittest.TestCase):
    """Fix Checking: fallback (in-memory) path works when Redis unavailable."""

    def test_returns_integer_without_redis(self):
        """Validates: Requirements 2.3 - works with Redis unavailable."""
        pq = _make_queue_no_redis()
        result = pq.get_queue_size()
        self.assertIsInstance(result, int)

    def test_empty_fallback_queue_returns_zero(self):
        """Returns 0 for empty in-memory queue."""
        pq = _make_queue_no_redis()
        self.assertEqual(pq.get_queue_size(), 0)

    def test_populated_fallback_queue_returns_accurate_count(self):
        """Returns accurate count from in-memory queue."""
        pq = _make_queue_no_redis()
        # Put items directly into the asyncio.Queue
        for _ in range(7):
            pq.fallback_queue.put_nowait(object())
        self.assertEqual(pq.get_queue_size(), 7)

    def test_result_non_negative_without_redis(self):
        """Queue size is always >= 0 in fallback mode."""
        pq = _make_queue_no_redis()
        self.assertGreaterEqual(pq.get_queue_size(), 0)


class TestGetQueueSizeReturnType(unittest.TestCase):
    """Validates: Requirements 2.3 - return type is always int."""

    def test_redis_path_returns_int_not_other_type(self):
        """llen returns various types; get_queue_size must always return int."""
        # Redis llen can return bytes or other numeric types in some clients
        for raw_value in [0, 5, 42]:
            with self.subTest(raw_value=raw_value):
                pq = _make_queue_with_redis(llen_return_value=raw_value)
                result = pq.get_queue_size()
                self.assertIsInstance(result, int)

    def test_fallback_path_returns_int(self):
        pq = _make_queue_no_redis()
        result = pq.get_queue_size()
        self.assertIsInstance(result, int)


if __name__ == "__main__":
    unittest.main()
