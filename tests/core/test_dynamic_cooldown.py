from contextlib import asynccontextmanager

import pytest

from src.core import dynamic_cooldown


class _Conn:
    def __init__(self, cursor=None):
        self.cursor = cursor
        self.executed = []

    async def fetchrow(self, _query, *_args):
        if self.cursor is None:
            return None
        return {"last_processed_id": self.cursor}

    async def execute(self, _query, *args):
        self.executed.append((_query, args))


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.mark.asyncio
async def test_record_dynamic_cooldown_default_resets_after_expiry(monkeypatch):
    monkeypatch.setattr(dynamic_cooldown.time, "time", lambda: 1000.0)
    conn = _Conn(cursor="900:3")

    state = await dynamic_cooldown.record_dynamic_cooldown(
        _Pool(conn),
        source="strava",
        scope="gps_streams",
        account="acct",
        base_seconds=10,
        max_seconds=100,
        jitter_ratio=0,
    )

    assert state.streak == 1
    assert state.seconds_remaining == 10


@pytest.mark.asyncio
async def test_record_dynamic_cooldown_memory_escalates_recently_expired(monkeypatch):
    monkeypatch.setattr(dynamic_cooldown.time, "time", lambda: 1000.0)
    conn = _Conn(cursor="900:3")

    state = await dynamic_cooldown.record_dynamic_cooldown(
        _Pool(conn),
        source="strava",
        scope="gps_streams",
        account="acct",
        base_seconds=10,
        max_seconds=100,
        jitter_ratio=0,
        memory_seconds=200,
    )

    assert state.streak == 4
    assert state.seconds_remaining == 80


@pytest.mark.asyncio
async def test_record_dynamic_cooldown_memory_ignores_old_expiry(monkeypatch):
    monkeypatch.setattr(dynamic_cooldown.time, "time", lambda: 1000.0)
    conn = _Conn(cursor="100:3")

    state = await dynamic_cooldown.record_dynamic_cooldown(
        _Pool(conn),
        source="strava",
        scope="gps_streams",
        account="acct",
        base_seconds=10,
        max_seconds=100,
        jitter_ratio=0,
        memory_seconds=200,
    )

    assert state.streak == 1
    assert state.seconds_remaining == 10
