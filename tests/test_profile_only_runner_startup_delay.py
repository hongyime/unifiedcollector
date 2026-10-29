"""Startup delay behavior for the profile-only runners."""
from __future__ import annotations

import asyncio
import os
import signal
import time

import pytest

from src.collectors import profile_only_runner as runner


class _FakePool:
    async def close(self) -> None:
        pass


class _RecordingCollector:
    SOURCE_NAME = "fake"
    enabled = True

    def __init__(self, on_cycle=None):
        self.calls: list[float] = []
        self._on_cycle = on_cycle
        self.pool = None

    def set_pool(self, pool):
        self.pool = pool

    async def run_cycle(self):
        self.calls.append(time.monotonic())
        if self._on_cycle is not None:
            self._on_cycle()
        return {"processed": 0}


def _install_fakes(monkeypatch, collector):
    async def fake_create_pool(*args, **kwargs):
        return _FakePool()

    monkeypatch.setattr(runner.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(runner, "_build_collectors", lambda: [collector])
    monkeypatch.setenv("DATABASE_URL", "postgres://test/test")


def _send_sigterm() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0.0),
        ("", 0.0),
        ("not-a-number", 0.0),
        ("-5", 0.0),
        ("nan", 0.0),
        ("inf", 0.0),
        ("-inf", 0.0),
        ("1e999", 0.0),
        ("2.5", 2.5),
        ("999999", runner._MAX_STARTUP_DELAY_SECONDS),
    ],
)
def test_startup_delay_parsing_is_bounded_and_safe(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("COLLECTOR_STARTUP_DELAY_SECONDS", raising=False)
    else:
        monkeypatch.setenv("COLLECTOR_STARTUP_DELAY_SECONDS", raw)

    assert runner._startup_delay_seconds() == expected


async def test_startup_delay_defers_first_cycle(monkeypatch):
    collector = _RecordingCollector(on_cycle=_send_sigterm)
    _install_fakes(monkeypatch, collector)
    monkeypatch.setenv("COLLECTOR_STARTUP_DELAY_SECONDS", "2")

    started = time.monotonic()
    await asyncio.wait_for(runner.run(), timeout=5.0)

    assert len(collector.calls) == 1
    assert collector.calls[0] - started >= 1.9


async def test_startup_delay_applies_only_before_first_cycle(monkeypatch):
    cycle_count = {"n": 0}

    def stop_after_second_cycle() -> None:
        cycle_count["n"] += 1
        if cycle_count["n"] == 2:
            _send_sigterm()

    collector = _RecordingCollector(on_cycle=stop_after_second_cycle)
    _install_fakes(monkeypatch, collector)
    monkeypatch.setenv("COLLECTOR_STARTUP_DELAY_SECONDS", "0.3")
    monkeypatch.setattr(runner, "_INTERVAL", 0.05)

    started = time.monotonic()
    await asyncio.wait_for(runner.run(), timeout=5.0)

    assert len(collector.calls) == 2
    assert collector.calls[0] - started >= 0.25
    assert collector.calls[1] - collector.calls[0] < 0.2


async def test_sigterm_during_startup_delay_exits_promptly(monkeypatch):
    collector = _RecordingCollector()
    _install_fakes(monkeypatch, collector)
    monkeypatch.setenv("COLLECTOR_STARTUP_DELAY_SECONDS", "30")

    loop = asyncio.get_running_loop()
    loop.call_later(0.2, _send_sigterm)

    started = time.monotonic()
    await asyncio.wait_for(runner.run(), timeout=3.0)

    assert time.monotonic() - started < 1.5
    assert collector.calls == []


async def test_invalid_delay_does_not_stall_first_cycle(monkeypatch):
    collector = _RecordingCollector(on_cycle=_send_sigterm)
    _install_fakes(monkeypatch, collector)
    monkeypatch.setenv("COLLECTOR_STARTUP_DELAY_SECONDS", "not-a-number")

    started = time.monotonic()
    await asyncio.wait_for(runner.run(), timeout=3.0)

    assert len(collector.calls) == 1
    assert collector.calls[0] - started < 1.0
