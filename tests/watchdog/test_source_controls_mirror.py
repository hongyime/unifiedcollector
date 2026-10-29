"""Watchdog per-source pass mirrors source_health into source_controls.

Stale sources become SOURCE_DEAD rows, an active HTTP 429 cooldown becomes a
RATE_LIMITED row with blocked_until, and recovered sources become HEALTHY.
The mirror is write-through and best-effort: _mark_degraded still does its
source_health upsert, and set_source_state never raises.
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("DATABASE_URL", "postgresql://collector:x@127.0.0.1:5999/unifiedcollector")

import pytest  # noqa: E402

from src.core.source_states import SourceState  # noqa: E402
from src.watchdog import freshness  # noqa: E402


class _FakeDB:
    def __init__(self, age):
        self._age = age
        self.executed = []

    async def fetchval(self, sql, *args):
        return self._age

    async def fetchrow(self, sql, *args):
        return None

    async def fetch(self, sql, *args):
        return []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


async def _noop(*args, **kwargs):
    return None


def _capture(monkeypatch):
    calls = []

    async def fake_set_source_state(pool, source, state, **kwargs):
        calls.append((source, state, kwargs))

    monkeypatch.setattr(freshness, "set_source_state", fake_set_source_state)
    return calls


def _prepare(monkeypatch, source, container, age):
    monkeypatch.setattr(freshness, "CHECKS", {source: ("SELECT 1", 10, [container])})
    monkeypatch.setenv("WATCHDOG_DECISION_BUTTONS_ENABLED", "0")
    monkeypatch.setattr(freshness, "_restart", _noop)
    monkeypatch.setattr(freshness, "_notify", _noop)
    monkeypatch.setattr(freshness, "_last_restart", {})
    monkeypatch.setattr(freshness, "_last_cooldown_stale_alert", {})
    monkeypatch.setattr(freshness, "_pending_decision_cards", {})
    return _FakeDB(age)


@pytest.mark.asyncio
async def test_stale_source_mirrors_source_dead(monkeypatch):
    calls = _capture(monkeypatch)
    db = _prepare(monkeypatch, "mirrorsrc", "c_mirror_dead", age=99999)

    await freshness._tick(db)

    assert len(calls) == 1
    source, state, kwargs = calls[0]
    assert (source, state) == ("mirrorsrc", SourceState.SOURCE_DEAD)
    assert "stale 99999s" in kwargs["stop_reason"]
    assert kwargs["blocked_until"] is None
    assert any("source_health" in sql for sql, _ in db.executed)


@pytest.mark.asyncio
async def test_429_cooldown_mirrors_rate_limited(monkeypatch):
    calls = _capture(monkeypatch)
    db = _prepare(monkeypatch, "mirrorsrc", "c_mirror_cooldown", age=99999)

    async def fake_cooldown(_db, _src, _now):
        return {"seconds_remaining": 600, "streak": None, "basis": "rate_limit_events"}

    monkeypatch.setattr(freshness, "_active_source_cooldown", fake_cooldown)
    started = time.time()

    await freshness._tick(db)

    assert len(calls) == 1
    _, state, kwargs = calls[0]
    assert state == SourceState.RATE_LIMITED
    assert kwargs["blocked_until"] is not None
    assert kwargs["blocked_until"].timestamp() >= started + 600


@pytest.mark.asyncio
async def test_fresh_source_mirrors_healthy(monkeypatch):
    calls = _capture(monkeypatch)
    db = _prepare(monkeypatch, "mirrorsrc", "c_mirror_healthy", age=5)

    await freshness._tick(db)

    assert calls == [("mirrorsrc", SourceState.HEALTHY, {})]
