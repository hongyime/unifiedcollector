"""Tests for src/core/matrix_backfill_state_repo.py.

Pure-unit. asyncpg pool is replaced with a stub that records SQL +
arguments without actually running anything. Confirms the repo issues
the right shape of SQL for get / fetch_pending / upsert_progress /
mark_done — and is robust to the row-not-yet-exists case for both
upserts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.matrix_backfill_state_repo import MatrixBackfillStateRepo


# ── stub pool / connection ────────────────────────────────────────────


class _Conn:
    def __init__(self, fetchrow_result=None, fetch_result=None):
        self._fetchrow_result = fetchrow_result
        self._fetch_result = fetch_result or []
        self.executed: list[tuple] = []
        self.fetched: list[tuple] = []
        self.fetchedrows: list[tuple] = []

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        return "INSERT 0 1"

    async def fetchrow(self, sql: str, *args):
        self.fetchedrows.append((sql, args))
        return self._fetchrow_result

    async def fetch(self, sql: str, *args):
        self.fetched.append((sql, args))
        return self._fetch_result


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


# ── construction ──────────────────────────────────────────────────────


def test_repo_requires_pool():
    with pytest.raises(ValueError):
        MatrixBackfillStateRepo(None)


# ── get ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_dict_when_row_present():
    fake_row = {
        "room_id": "!r:b",
        "last_token": "tok",
        "earliest_ts": None,
        "events_fetched": 5,
        "pages_used": 2,
        "done": False,
        "last_error": None,
        "last_attempt_at": None,
        "completed_at": None,
    }
    conn = _Conn(fetchrow_result=fake_row)
    repo = MatrixBackfillStateRepo(_Pool(conn))
    out = await repo.get("!r:b")
    assert out["room_id"] == "!r:b"
    assert out["events_fetched"] == 5
    assert len(conn.fetchedrows) == 1
    sql, args = conn.fetchedrows[0]
    assert "matrix_backfill_state" in sql
    assert args == ("!r:b",)


@pytest.mark.asyncio
async def test_get_returns_none_when_missing():
    conn = _Conn(fetchrow_result=None)
    repo = MatrixBackfillStateRepo(_Pool(conn))
    out = await repo.get("!nope:b")
    assert out is None


# ── fetch_pending ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_pending_returns_list_of_dicts():
    rows = [
        {"room_id": "!a:b", "last_token": None, "earliest_ts": None,
         "events_fetched": 0, "pages_used": 0, "done": False,
         "last_error": None, "last_attempt_at": None, "completed_at": None},
        {"room_id": "!b:b", "last_token": "t", "earliest_ts": None,
         "events_fetched": 50, "pages_used": 1, "done": False,
         "last_error": None, "last_attempt_at": None, "completed_at": None},
    ]
    conn = _Conn(fetch_result=rows)
    repo = MatrixBackfillStateRepo(_Pool(conn))
    out = await repo.fetch_pending(limit=10)
    assert len(out) == 2
    assert out[0]["room_id"] == "!a:b"
    sql, args = conn.fetched[0]
    assert "done = FALSE" in sql
    assert args == (10,)


@pytest.mark.asyncio
async def test_fetch_pending_zero_limit_short_circuits():
    conn = _Conn()
    repo = MatrixBackfillStateRepo(_Pool(conn))
    out = await repo.fetch_pending(limit=0)
    assert out == []
    assert conn.fetched == []  # never hit the DB


# ── upsert_progress ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_progress_writes_increments_and_token():
    conn = _Conn()
    repo = MatrixBackfillStateRepo(_Pool(conn))
    await repo.upsert_progress(
        room_id="!r:b",
        last_token="tok-2",
        events_fetched_inc=42,
        pages_inc=1,
        error=None,
    )
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    assert "INSERT INTO matrix_backfill_state" in sql
    assert "ON CONFLICT (room_id) DO UPDATE" in sql
    # args order matches the SQL: room_id, last_token, earliest_ts,
    # events_fetched_inc, pages_inc, error, now
    assert args[0] == "!r:b"
    assert args[1] == "tok-2"
    assert args[3] == 42
    assert args[4] == 1
    assert args[5] is None


@pytest.mark.asyncio
async def test_upsert_progress_records_error_string():
    conn = _Conn()
    repo = MatrixBackfillStateRepo(_Pool(conn))
    await repo.upsert_progress(
        room_id="!r:b",
        last_token=None,
        events_fetched_inc=0,
        pages_inc=1,
        error="RuntimeError: boom",
    )
    sql, args = conn.executed[0]
    assert args[5] == "RuntimeError: boom"


@pytest.mark.asyncio
async def test_upsert_progress_accepts_earliest_ts():
    conn = _Conn()
    repo = MatrixBackfillStateRepo(_Pool(conn))
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    await repo.upsert_progress(
        room_id="!r:b",
        last_token="t",
        events_fetched_inc=1,
        pages_inc=1,
        earliest_ts=ts,
    )
    sql, args = conn.executed[0]
    assert args[2] == ts


# ── mark_done ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_done_sets_done_flag_via_upsert():
    conn = _Conn()
    repo = MatrixBackfillStateRepo(_Pool(conn))
    await repo.mark_done("!r:b")
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    assert "done            = TRUE" in sql or "done = TRUE" in sql
    assert "ON CONFLICT (room_id) DO UPDATE" in sql
    assert args[0] == "!r:b"
