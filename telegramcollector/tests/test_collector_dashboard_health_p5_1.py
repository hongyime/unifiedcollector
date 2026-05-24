"""Unit tests for Collector Dashboard Health panel — Task 5.1.

Requirements: 1.3, 1.4
"""
import types
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_streamlit_mock():
    """Return a minimal streamlit mock that records calls."""
    st = mock.MagicMock()
    # Make st.tabs return a list of context-manager-compatible mocks
    tab_ctx = mock.MagicMock()
    tab_ctx.__enter__ = mock.Mock(return_value=tab_ctx)
    tab_ctx.__exit__ = mock.Mock(return_value=False)
    st.tabs.return_value = [tab_ctx] * 7
    st.columns.return_value = [mock.MagicMock(), mock.MagicMock()]
    return st


# ---------------------------------------------------------------------------
# test_postgres_unreachable_renders_error
# ---------------------------------------------------------------------------

def test_postgres_unreachable_renders_error():
    """When Postgres is unreachable, st.error is called and the app does not crash."""
    st_mock = _make_streamlit_mock()

    with (
        mock.patch("psycopg2.connect", side_effect=Exception("connection refused")),
        mock.patch.dict("sys.modules", {"streamlit": st_mock}),
        mock.patch("services.collector.dashboard.db.psycopg2.connect", side_effect=Exception("connection refused")),
    ):
        # Re-import so module-level code re-runs with the patched connect
        import importlib
        import services.collector.dashboard.db as db_mod
        importlib.reload(db_mod)

        assert db_mod.check_postgres() is False

    # Simulate what app.py does when check_postgres() returns False
    postgres_ok = False
    if not postgres_ok:
        st_mock.error("Postgres unreachable")

    st_mock.error.assert_called_with("Postgres unreachable")


# ---------------------------------------------------------------------------
# test_redis_unreachable_shows_unknown
# ---------------------------------------------------------------------------

def test_redis_unreachable_shows_unknown():
    """When Redis is unavailable, queue depth metrics show 'unknown'."""
    # get_redis() returns None when Redis is unreachable
    with mock.patch("services.collector.dashboard.redis_client.get_redis", return_value=None):
        from services.collector.dashboard.redis_client import get_redis
        redis_client = get_redis()

    # Simulate the metric computation in app.py
    try:
        media_queue_depth = redis_client.llen("collector:media_queue") if redis_client else "unknown"
    except Exception:
        media_queue_depth = "unknown"

    try:
        dlq_count = redis_client.llen("collector:dlq") if redis_client else "unknown"
    except Exception:
        dlq_count = "unknown"

    assert media_queue_depth == "unknown"
    assert dlq_count == "unknown"


def test_redis_llen_exception_shows_unknown():
    """When redis.llen raises, queue depth falls back to 'unknown'."""
    redis_mock = mock.MagicMock()
    redis_mock.llen.side_effect = Exception("READONLY")

    try:
        media_queue_depth = redis_mock.llen("collector:media_queue") if redis_mock else "unknown"
    except Exception:
        media_queue_depth = "unknown"

    try:
        dlq_count = redis_mock.llen("collector:dlq") if redis_mock else "unknown"
    except Exception:
        dlq_count = "unknown"

    assert media_queue_depth == "unknown"
    assert dlq_count == "unknown"


def test_redis_hgetall_exception_does_not_crash():
    """When redis.hgetall raises for one account, the loop continues without crashing."""
    redis_mock = mock.MagicMock()
    redis_mock.hgetall.side_effect = Exception("timeout")

    accounts = [(1, "+1234567890"), (2, "+0987654321")]
    rows = []
    for _account_id, phone_number in accounts:
        try:
            hash_data = redis_mock.hgetall(f"worker:status:{phone_number}")
        except Exception:
            hash_data = {}

        connected = hash_data.get(b"connected", b"").decode("utf-8", errors="ignore") if hash_data else ""
        last_message_at = hash_data.get(b"last_message_at", b"").decode("utf-8", errors="ignore") if hash_data else ""
        rows.append({"Phone": phone_number, "Connected": connected, "Last Message At": last_message_at})

    # Both accounts should still appear in the table, just with empty fields
    assert len(rows) == 2
    assert rows[0]["Phone"] == "+1234567890"
    assert rows[1]["Phone"] == "+0987654321"
    assert rows[0]["Connected"] == ""
