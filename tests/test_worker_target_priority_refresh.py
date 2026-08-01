from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest


class _Conn:
    async def fetch(self, *_args, **_kwargs):
        return []


class _Pool:
    def acquire(self):
        @asynccontextmanager
        async def _cm():
            yield _Conn()

        return _cm()


@pytest.mark.asyncio
async def test_realtime_sources_skip_expensive_priority_refresh(monkeypatch):
    import src.worker as worker_mod

    svc = worker_mod.WorkerService()
    svc.pool = _Pool()
    proximity = AsyncMock()
    hints = AsyncMock()
    ensure = AsyncMock()
    monkeypatch.setattr(worker_mod, "refresh_account_proximity_cache", proximity)
    monkeypatch.setattr(worker_mod, "refresh_collector_priority_hints", hints)
    monkeypatch.setattr(worker_mod, "ensure_account_proximity_cache", ensure)

    assert await svc._load_targets("beeper") == []

    proximity.assert_not_awaited()
    hints.assert_not_awaited()
    ensure.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_realtime_sources_refresh_priority_inputs(monkeypatch):
    import src.worker as worker_mod

    svc = worker_mod.WorkerService()
    svc.pool = _Pool()
    proximity = AsyncMock()
    hints = AsyncMock()
    ensure = AsyncMock()
    monkeypatch.setattr(worker_mod, "refresh_account_proximity_cache", proximity)
    monkeypatch.setattr(worker_mod, "refresh_collector_priority_hints", hints)
    monkeypatch.setattr(worker_mod, "ensure_account_proximity_cache", ensure)

    assert await svc._load_targets("instagram") == []

    proximity.assert_awaited_once()
    hints.assert_awaited_once()
    ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_realtime_zero_progress_return_does_not_mark_source_healthy(monkeypatch):
    import src.worker as worker_mod

    class _FakeCollector:
        progress_count = 0
        intentional_idle_reason = None

        def set_pool(self, _pool):
            pass

        async def run(self, _targets):
            svc._stop.set()

    svc = worker_mod.WorkerService()
    svc.pool = _Pool()
    svc._stop = asyncio.Event()
    svc._collectors = {}
    svc._progress_baseline = {}
    svc._last_success_progress = {}
    svc._zero_progress_streak = {}
    svc._crash_counts = {"whatsapp": 0}
    svc._hang_counts = {}
    svc._heartbeat = {}
    svc._auth_paused = {}
    svc._auth_pause_since = {}
    monkeypatch.setattr(worker_mod, "get_collector", lambda _source: _FakeCollector())
    monkeypatch.setattr(svc, "_load_targets", AsyncMock(return_value=[]))
    mark_healthy = AsyncMock()
    monkeypatch.setattr(svc, "_mark_source_healthy", mark_healthy)

    await svc._run_source("whatsapp")

    mark_healthy.assert_not_awaited()
