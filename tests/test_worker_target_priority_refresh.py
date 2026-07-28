from __future__ import annotations

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
