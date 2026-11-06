from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.scheduler.handlers import memory_recovery
from src.scheduler.handlers.memory_recovery import MemoryRecoveryHandler, _available_pct
from src import control_executor


class _DB:
    def __init__(self):
        self.state = None
        self.actions = []

    async def fetchrow(self, sql, *args):
        if "FROM memory_recovery_state" in sql:
            return self.state
        if "INSERT INTO memory_recovery_actions" in sql:
            self.actions.append({"target": args[0], "action": args[1]})
            return {"id": len(self.actions)}
        return None

    async def fetch(self, _sql, *_args):
        return []

    async def fetchval(self, sql, *_args):
        if "memory_recovery_actions WHERE status" in sql:
            return False
        return False

    async def execute(self, sql, *_args):
        if "INSERT INTO memory_recovery_state" in sql:
            self.state = {"pressure_since": None, "recovery_since": None, "paused_target": None,
                          "cooldown_until": None, "circuit_open_until": None}
        elif "pressure_since=COALESCE" in sql:
            self.state["pressure_since"] = self.state["pressure_since"] or _NOW[0]
            self.state["recovery_since"] = None

    def transaction(self):
        return _CM(self)


class _CM:
    def __init__(self, value): self.value = value
    async def __aenter__(self): return self.value
    async def __aexit__(self, *_args): return False


class _Pool:
    def __init__(self, db): self.db = db
    def acquire(self): return _CM(self.db)


_NOW = [None]


def _ctx(db, now):
    _NOW[0] = now
    values = {
        "MEMORY_RECOVERY_PAUSE_AT_PCT": 10.0,
        "MEMORY_RECOVERY_RESUME_AT_PCT": 20.0,
        "MEMORY_RECOVERY_SUSTAINED_SECONDS": 60,
        "MEMORY_RECOVERY_CANCEL_GRACE_SECONDS": 60,
        "MEMORY_RECOVERY_COOLDOWN_SECONDS": 300,
        "MEMORY_RECOVERY_CIRCUIT_SECONDS": 600,
    }
    class Context:
        pool = _Pool(db)
        stop_event = asyncio.Event()
        get_env_int = staticmethod(lambda name, default, **_kw: int(values.get(name, default)))
        get_env_float = staticmethod(lambda name, default, **_kw: float(values.get(name, default)))
    Context.now = now
    return Context()


def test_available_pct_requires_total_memory():
    assert _available_pct({"MemTotal": 100, "MemAvailable": 25}) == 25
    assert _available_pct({"MemAvailable": 25}) is None


def test_recovery_never_targets_financial_or_messaging_collectors():
    assert "unifiedcollector_collector_whatsapp" not in memory_recovery._ELIGIBLE


@pytest.mark.asyncio
async def test_sustained_pressure_schedules_one_lowest_priority_non_realtime_collector(monkeypatch):
    db = _DB()
    handler = MemoryRecoveryHandler()
    monkeypatch.setattr(memory_recovery, "_read_meminfo", lambda: {"MemTotal": 1000, "MemAvailable": 50})
    async def card(_db, *_args): return "token"
    async def quiet(*_args): return None
    monkeypatch.setattr("src.notifications.collector_bot.create_memory_recovery_card", card)
    monkeypatch.setattr(handler, "_announce", quiet)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await handler.run(_ctx(db, start))
    await handler.run(_ctx(db, start + timedelta(seconds=61)))
    await asyncio.sleep(0)
    assert db.actions == [{"target": "unifiedcollector_collector_airbnb", "action": "pause"}]
