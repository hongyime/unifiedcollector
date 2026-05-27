"""Tests for src/core/matrix_dedupe_queries.py.

Pure-unit. asyncpg pool is replaced with a stub that records SQL +
arguments. No database required.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.core import matrix_dedupe_queries as mdq


# ── stub pool / connection ────────────────────────────────────────────


class _Conn:
    def __init__(self):
        self.executed: list[tuple] = []
        self.fetched: list[tuple] = []
        self.fetchedrows: list[tuple] = []
        self.fetchvals: list[tuple] = []
        # Configurable return values keyed by SQL substring.
        self._fetchrow_responder = lambda sql, args: None
        self._fetch_responder = lambda sql, args: []
        self._fetchval_responder = lambda sql, args: 0

    async def fetchrow(self, sql: str, *args):
        self.fetchedrows.append((sql, args))
        return self._fetchrow_responder(sql, args)

    async def fetch(self, sql: str, *args):
        self.fetched.append((sql, args))
        return self._fetch_responder(sql, args)

    async def fetchval(self, sql: str, *args):
        self.fetchvals.append((sql, args))
        return self._fetchval_responder(sql, args)


class _Pool:
    def __init__(self, conn: _Conn):
        self.conn = conn

    def acquire(self):
        pool_self = self
        class _Ctx:
            async def __aenter__(self_inner):
                return pool_self.conn
            async def __aexit__(self_inner, *exc):
                return False
        return _Ctx()


def _table_exists_responder(present: set[str]):
    def _r(sql, args):
        if "information_schema.tables" in sql and args:
            return args[0] in present
        return False
    return _r


# ── _clamp_limit ──────────────────────────────────────────────────────


def test_clamp_limit_floors_zero_to_one():
    assert mdq._clamp_limit(0) == 1
    assert mdq._clamp_limit(-5) == 1


def test_clamp_limit_caps_to_max():
    assert mdq._clamp_limit(10_000) == mdq._MAX_LIMIT


def test_clamp_limit_passes_through():
    assert mdq._clamp_limit(50) == 50


# ── find_matrix_twin_telegram ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_telegram_twin_returns_match():
    ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    conn = _Conn()
    matrix_row = {
        "event_id": "$ev1",
        "room_id": "!r:b",
        "sender": "@telegram_123:beeper.local",
        "body": "hello",
        "server_ts": ts,
        "msgtype": "m.text",
    }

    def _fetchrow(sql, args):
        if "telegram_messages" in sql:
            return {"text": "hello", "platform_created_at": ts}
        return None

    def _fetch(sql, args):
        if "matrix_events" in sql:
            return [matrix_row]
        return []

    conn._fetchrow_responder = _fetchrow
    conn._fetch_responder = _fetch
    out = await mdq.find_matrix_twin_telegram(_Pool(conn), "tg_chat", "tg_msg")
    assert len(out) == 1
    assert out[0]["event_id"] == "$ev1"
    # Confirm parameterized
    sql, args = conn.fetched[-1]
    assert "$1" in sql and "$2" in sql and "$3" in sql and "$4" in sql
    assert args[0] == "hello"


@pytest.mark.asyncio
async def test_find_telegram_twin_returns_empty_when_no_native_row():
    conn = _Conn()
    conn._fetchrow_responder = lambda sql, args: None
    out = await mdq.find_matrix_twin_telegram(_Pool(conn), "x", "y")
    assert out == []


@pytest.mark.asyncio
async def test_find_telegram_twin_handles_missing_table():
    conn = _Conn()

    async def _raise(*a, **kw):
        raise RuntimeError("relation telegram_messages does not exist")
    conn.fetchrow = _raise  # type: ignore
    out = await mdq.find_matrix_twin_telegram(_Pool(conn), "x", "y")
    assert out == []


@pytest.mark.asyncio
async def test_find_telegram_twin_skips_when_body_empty():
    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    conn = _Conn()
    conn._fetchrow_responder = lambda sql, args: {"text": "", "platform_created_at": ts}
    out = await mdq.find_matrix_twin_telegram(_Pool(conn), "x", "y")
    assert out == []


# ── find_matrix_twin_whatsapp ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_whatsapp_twin_returns_match():
    ts = datetime(2025, 5, 1, tzinfo=timezone.utc)
    conn = _Conn()
    matrix_row = {
        "event_id": "$ev2",
        "room_id": "!r2:b",
        "sender": "@whatsapp_456:beeper.local",
        "body": "hi",
        "server_ts": ts,
        "msgtype": "m.text",
    }
    conn._fetchrow_responder = lambda sql, args: {"text": "hi", "timestamp": ts}
    conn._fetch_responder = lambda sql, args: [matrix_row]
    out = await mdq.find_matrix_twin_whatsapp(_Pool(conn), "1@s.whatsapp.net", "wa_msg")
    assert len(out) == 1
    assert out[0]["event_id"] == "$ev2"


@pytest.mark.asyncio
async def test_find_whatsapp_twin_handles_missing_table():
    conn = _Conn()

    async def _raise(*a, **kw):
        raise RuntimeError("relation whatsapp_messages does not exist")
    conn.fetchrow = _raise  # type: ignore
    out = await mdq.find_matrix_twin_whatsapp(_Pool(conn), "x", "y")
    assert out == []


# ── matrix_only_events ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_matrix_only_events_with_both_native_tables():
    conn = _Conn()
    conn._fetchval_responder = _table_exists_responder(
        {"telegram_messages", "whatsapp_messages"}
    )
    conn._fetch_responder = lambda sql, args: [
        {"event_id": "$o1", "room_id": "!r:b", "sender": "@bob:b",
         "body": "orphan", "server_ts": datetime.now(timezone.utc),
         "msgtype": "m.text"}
    ]
    out = await mdq.matrix_only_events(_Pool(conn), limit=10)
    assert len(out) == 1
    sql, _ = conn.fetched[-1]
    assert "telegram_messages" in sql
    assert "whatsapp_messages" in sql
    assert "LIMIT" in sql.upper()


@pytest.mark.asyncio
async def test_matrix_only_events_when_no_native_tables():
    conn = _Conn()
    conn._fetchval_responder = _table_exists_responder(set())
    conn._fetch_responder = lambda sql, args: []
    out = await mdq.matrix_only_events(_Pool(conn))
    assert out == []
    sql, _ = conn.fetched[-1]
    # No NOT EXISTS subqueries when no native tables present
    assert "telegram_messages" not in sql
    assert "whatsapp_messages" not in sql


@pytest.mark.asyncio
async def test_matrix_only_events_with_since_ts():
    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    conn = _Conn()
    conn._fetchval_responder = _table_exists_responder({"telegram_messages"})
    conn._fetch_responder = lambda sql, args: []
    await mdq.matrix_only_events(_Pool(conn), since_ts=ts, limit=5)
    sql, args = conn.fetched[-1]
    assert "server_ts >=" in sql
    assert ts in args


@pytest.mark.asyncio
async def test_matrix_only_events_handles_pool_failure():
    class _BadPool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    raise RuntimeError("db down")
                async def __aexit__(self_inner, *exc):
                    return False
            return _Ctx()
    out = await mdq.matrix_only_events(_BadPool())
    assert out == []


# ── coverage_overlap_summary ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_coverage_summary_both_present():
    conn = _Conn()
    counts = {
        "telegram_total": 100,
        "telegram_twin": 80,
        "whatsapp_total": 50,
        "whatsapp_twin": 40,
        "orphan": 7,
    }

    def _fv(sql, args):
        if "information_schema.tables" in sql:
            return args[0] in {"telegram_messages", "whatsapp_messages"}
        # matrix_only subquery: distinct by SELECT COUNT(*) FROM (
        if "COUNT(*) FROM (" in sql:
            return counts["orphan"]
        if "FROM telegram_messages tm" in sql and "EXISTS" in sql:
            return counts["telegram_twin"]
        if "FROM telegram_messages" in sql:
            return counts["telegram_total"]
        if "FROM whatsapp_messages wm" in sql and "EXISTS" in sql:
            return counts["whatsapp_twin"]
        if "FROM whatsapp_messages" in sql:
            return counts["whatsapp_total"]
        return 0

    conn._fetchval_responder = _fv
    out = await mdq.coverage_overlap_summary(_Pool(conn))
    assert out["telegram"]["available"] is True
    assert out["telegram"]["total"] == 100
    assert out["telegram"]["with_matrix_twin"] == 80
    assert out["telegram"]["matrix_only"] == 7
    assert out["whatsapp"]["available"] is True
    assert out["whatsapp"]["total"] == 50
    assert out["whatsapp"]["with_matrix_twin"] == 40
    assert out["whatsapp"]["matrix_only"] == 7


@pytest.mark.asyncio
async def test_coverage_summary_no_native_tables():
    conn = _Conn()
    conn._fetchval_responder = _table_exists_responder(set())
    out = await mdq.coverage_overlap_summary(_Pool(conn))
    assert out["telegram"] == {"total": 0, "with_matrix_twin": 0, "matrix_only": 0, "available": False}
    assert out["whatsapp"] == {"total": 0, "with_matrix_twin": 0, "matrix_only": 0, "available": False}


@pytest.mark.asyncio
async def test_coverage_summary_telegram_only():
    conn = _Conn()

    def _fv(sql, args):
        if "information_schema.tables" in sql:
            return args[0] == "telegram_messages"
        if "COUNT(*) FROM (" in sql:
            return 2
        if "FROM telegram_messages tm" in sql and "EXISTS" in sql:
            return 5
        if "FROM telegram_messages" in sql:
            return 10
        return 0

    conn._fetchval_responder = _fv
    out = await mdq.coverage_overlap_summary(_Pool(conn))
    assert out["telegram"]["available"] is True
    assert out["telegram"]["total"] == 10
    assert out["whatsapp"]["available"] is False


@pytest.mark.asyncio
async def test_coverage_summary_handles_pool_failure():
    class _BadPool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    raise RuntimeError("nope")
                async def __aexit__(self_inner, *exc):
                    return False
            return _Ctx()
    out = await mdq.coverage_overlap_summary(_BadPool())
    # Default skeleton is returned
    assert "telegram" in out and "whatsapp" in out
    assert out["telegram"]["available"] is False
    assert out["whatsapp"]["available"] is False


# ── _table_exists ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_table_exists_true():
    conn = _Conn()
    conn._fetchval_responder = lambda sql, args: True
    assert await mdq._table_exists(conn, "telegram_messages") is True


@pytest.mark.asyncio
async def test_table_exists_handles_error():
    conn = _Conn()

    async def _raise(*a, **kw):
        raise RuntimeError("bad")
    conn.fetchval = _raise  # type: ignore
    assert await mdq._table_exists(conn, "x") is False
