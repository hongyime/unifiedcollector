"""Bounded retry for profile_probe_queue (T15).

Transient failures (timeouts, 5xx, 429, challenge pages) re-enqueue with
exponential backoff; persistent failures stop at PROFILE_PROBE_MAX_ATTEMPTS.
A confirmed 404 is terminal on the first probe.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from src.core.profile_only_collector import ProfileOnlyCollector, ProfileResult


class _Conn:
    def __init__(self, rows=None, attempts=None):
        self.rows = list(rows or [])
        self.attempts = dict(attempts or {})
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self.rows

    async def fetchval(self, sql, *args):
        key = (args[0], args[1])
        self.attempts[key] = self.attempts.get(key, 0) + 1
        return self.attempts[key]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


class _Pool:
    def __init__(self, conn: _Conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class _Collector(ProfileOnlyCollector):
    SOURCE_NAME = "snapchat"
    PROFILE_TABLE = "snapchat_profiles"

    def __init__(self, conn: _Conn):
        super().__init__()
        self.pool = _Pool(conn)
        self.probe = None

    async def probe_profile(self, username: str) -> ProfileResult:
        return await self.probe(username)


def _sql_shape(sql: str) -> str:
    return " ".join(sql.split()).lower()


@pytest.fixture(autouse=True)
def _fast_and_defaults(monkeypatch):
    # Keep the per-probe rate-limit sleep out of the test budget.
    monkeypatch.setenv("SNAPCHAT_PROBE_QPS", "1000")
    monkeypatch.delenv("PROFILE_PROBE_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("PROFILE_PROBE_BACKOFF_BASE_SECONDS", raising=False)


async def test_first_timeout_requeues_with_backoff() -> None:
    conn = _Conn()
    coll = _Collector(conn)

    await coll._mark_attempt("snapchat", "alice", "http:TimeoutException", transient=True)

    assert conn.attempts[("snapchat", "alice")] == 1
    sql, args = conn.executed[-1]
    assert "status='pending'" in sql.replace(" ", "")
    assert "next_probe_at" in sql
    assert args == ("snapchat", "alice", 3600)


async def test_five_consecutive_failures_are_terminal() -> None:
    conn = _Conn()
    coll = _Collector(conn)
    delays = []

    for _ in range(5):
        await coll._mark_attempt("snapchat", "alice", "http:503", transient=True)
        sql, args = conn.executed[-1]
        if "status='error'" in sql.replace(" ", ""):
            delays.append(None)
        else:
            delays.append(args[2])

    assert conn.attempts[("snapchat", "alice")] == 5
    assert delays == [3600, 7200, 14400, 28800, None]
    assert "status='error'" in conn.executed[-1][0].replace(" ", "")


async def test_non_transient_error_is_terminal_immediately() -> None:
    conn = _Conn()
    coll = _Collector(conn)

    await coll._mark_attempt("snapchat", "alice", "http:403", transient=False)

    assert conn.attempts[("snapchat", "alice")] == 1
    assert "status='error'" in conn.executed[-1][0].replace(" ", "")


async def test_backoff_is_capped(monkeypatch) -> None:
    monkeypatch.setenv("PROFILE_PROBE_MAX_ATTEMPTS", "10")
    conn = _Conn(attempts={("snapchat", "alice"): 5})
    coll = _Collector(conn)

    await coll._mark_attempt("snapchat", "alice", "http:503", transient=True)

    # 3600 * 2**5 = 115200 -> capped at 86400.
    assert conn.attempts[("snapchat", "alice")] == 6
    assert conn.executed[-1][1] == ("snapchat", "alice", 86400)


async def test_run_cycle_only_selects_due_pending_rows() -> None:
    conn = _Conn(rows=[])
    coll = _Collector(conn)

    await coll.run_cycle()

    sql = _sql_shape(conn.fetch_calls[0][0])
    assert "status = 'pending'" in sql
    assert "next_probe_at is null or next_probe_at <= now()" in sql


async def test_run_cycle_timeout_requeues() -> None:
    conn = _Conn(rows=[{"id": 7, "username": "alice"}])
    coll = _Collector(conn)

    async def _timeout(_username):
        raise asyncio.TimeoutError()

    coll.probe = _timeout

    summary = await coll.run_cycle()

    assert summary["errors"] == 1
    sql, args = conn.executed[-1]
    assert "status='pending'" in sql.replace(" ", "")
    assert args == ("snapchat", "alice", 3600)


async def test_run_cycle_404_is_terminal_without_attempts() -> None:
    conn = _Conn(rows=[{"id": 7, "username": "alice"}])
    coll = _Collector(conn)

    async def _missing(username):
        return ProfileResult(exists=False, username=username, fields={})

    coll.probe = _missing

    summary = await coll.run_cycle()

    assert summary["not_found"] == 1
    assert conn.attempts == {}
    sql, _args = conn.executed[-1]
    assert "status='404'" in sql.replace(" ", "")


async def test_run_cycle_4xx_error_is_terminal() -> None:
    conn = _Conn(rows=[{"id": 7, "username": "alice"}])
    coll = _Collector(conn)

    async def _forbidden(username):
        return ProfileResult(exists=False, username=username, fields={}, error="http:403")

    coll.probe = _forbidden

    summary = await coll.run_cycle()

    assert summary["errors"] == 1
    sql, _args = conn.executed[-1]
    assert "status='error'" in sql.replace(" ", "")
