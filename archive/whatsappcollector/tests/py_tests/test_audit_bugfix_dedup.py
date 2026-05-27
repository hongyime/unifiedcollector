"""Tests for the _check_dedup() helper refactor: BUG-3."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from shared.circuit_breaker import CircuitOpenError


# ---------------------------------------------------------------------------
# Helpers — build a minimal Worker with controlled broker state
# ---------------------------------------------------------------------------

def _make_worker(broker_type: str = "redis", redis_client=None):
    """Instantiate Worker with mocked internals to avoid real connections."""
    with patch("services.collector.collector.worker.BrokerManager"), \
         patch("services.collector.collector.worker.DLQProcessor"), \
         patch("services.collector.collector.worker.backfill_manager"), \
         patch("services.collector.collector.worker.session_health_monitor"):
        from services.collector.collector.worker import Worker
        w = Worker()
    w.broker._broker = MagicMock()
    w.broker._broker.redis = redis_client

    # Patch settings.BROKER_TYPE
    import services.collector.collector.worker as wmod
    wmod.settings.BROKER_TYPE = broker_type
    return w


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestCheckDedupNotRedis:
    def test_returns_false_when_broker_type_not_redis(self):
        worker = _make_worker(broker_type="rabbitmq")
        result = _run(worker._check_dedup("msg1", "chat1", "messages.inbound"))
        assert result is False


class TestCheckDedupNewKey:
    def test_returns_false_when_redis_set_returns_true(self):
        """Redis returns True (nx=True set succeeded) → not a duplicate."""
        redis_mock = MagicMock()
        redis_mock.set = AsyncMock(return_value=True)
        worker = _make_worker(broker_type="redis", redis_client=redis_mock)
        result = _run(worker._check_dedup("msg1", "chat1", "messages.inbound"))
        assert result is False


class TestCheckDedupExistingKey:
    def test_returns_true_when_redis_set_returns_none(self):
        """Redis returns None (key already exists) → duplicate."""
        redis_mock = MagicMock()
        redis_mock.set = AsyncMock(return_value=None)
        worker = _make_worker(broker_type="redis", redis_client=redis_mock)
        result = _run(worker._check_dedup("msg1", "chat1", "messages.inbound"))
        assert result is True


class TestCheckDedupCircuitOpen:
    def test_returns_false_and_logs_warning_when_circuit_open(self):
        redis_mock = MagicMock()
        redis_mock.set = AsyncMock(side_effect=Exception("redis fail"))
        worker = _make_worker(broker_type="redis", redis_client=redis_mock)

        # Force CircuitOpenError from the circuit breaker
        async def raise_circuit_open(_):
            raise CircuitOpenError("collector_redis_dedup")

        worker._dedup_circuit.call = raise_circuit_open

        with patch("services.collector.collector.worker.logger") as mock_logger:
            result = _run(worker._check_dedup("msg1", "chat1", "messages.inbound"))

        assert result is False
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args[0][0]
        assert "dedup_circuit_open" in call_args


class TestCheckDedupRedisException:
    def test_returns_false_and_logs_warning_on_redis_error(self):
        redis_mock = MagicMock()
        redis_mock.set = AsyncMock(side_effect=Exception("connection reset"))
        worker = _make_worker(broker_type="redis", redis_client=redis_mock)

        # Circuit breaker passes through — exception propagates to _check_dedup handler
        async def passthrough(fn):
            return await fn()

        worker._dedup_circuit.call = passthrough

        with patch("services.collector.collector.worker.logger") as mock_logger:
            result = _run(worker._check_dedup("msg1", "chat1", "messages.inbound"))

        assert result is False
        mock_logger.warning.assert_called()


class TestCheckDedupNoRedisClient:
    def test_returns_false_when_redis_client_is_none(self):
        worker = _make_worker(broker_type="redis", redis_client=None)
        result = _run(worker._check_dedup("msg1", "chat1", "messages.inbound"))
        assert result is False


# ---------------------------------------------------------------------------
# Property-based test: output must match original inline logic
# ---------------------------------------------------------------------------

try:
    from hypothesis import given, settings as h_settings
    from hypothesis import strategies as st
    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False


if HAS_HYPOTHESIS:
    @given(
        message_id=st.text(min_size=1, max_size=64),
        chat_jid=st.text(min_size=1, max_size=64),
        redis_returns=st.one_of(st.just(True), st.just(None)),
    )
    @h_settings(max_examples=100)
    def test_check_dedup_matches_inline_logic(message_id, chat_jid, redis_returns):
        """_check_dedup result must match what the original inline block would have produced."""
        redis_mock = MagicMock()
        redis_mock.set = AsyncMock(return_value=redis_returns)
        worker = _make_worker(broker_type="redis", redis_client=redis_mock)

        result = _run(worker._check_dedup(message_id, chat_jid, "messages.inbound"))
        # Original logic: duplicate = not was_set  (was_set == redis return value)
        expected_duplicate = not redis_returns
        assert result == expected_duplicate
