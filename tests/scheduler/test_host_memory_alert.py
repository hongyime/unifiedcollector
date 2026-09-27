"""Tests for HostMemoryAlertHandler — host/VM memory pressure alert.

TDD: these tests are written BEFORE the implementation exists. They must
go RED first, then GREEN once the handler is implemented.

Three scenarios:
  S1 (happy-path / alert fires): MemAvailable/MemTotal < threshold → alert sent.
  S2 (healthy / no alert): MemAvailable/MemTotal >= threshold → no alert sent.
  S3 (cooldown / dedup): alert already fired recently → should_run returns False,
      no second alert sent within the cooldown window.
"""
from __future__ import annotations

import asyncio
import time as _time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(*, threshold_pct: int = 15, interval_seconds: int = 3600):
    """Return a minimal SchedulerContext stub with injected env-var values."""
    from src.scheduler.handlers.base import SchedulerContext

    def _get_env_int(name: str, default: int, *, min_value: int = 0) -> int:
        mapping = {
            "HOST_MEMORY_ALERT_THRESHOLD_PCT": threshold_pct,
            "HOST_MEMORY_ALERT_INTERVAL_SECONDS": interval_seconds,
        }
        return max(min_value, mapping.get(name, default))

    ctx = MagicMock(spec=SchedulerContext)
    ctx.get_env_int = _get_env_int
    ctx.pool = None  # handler does not use the DB pool
    return ctx


def _meminfo_content(total_kb: int, available_kb: int) -> str:
    """Minimal /proc/meminfo text with the two keys the handler reads."""
    return (
        f"MemTotal:       {total_kb} kB\n"
        f"MemFree:         1000 kB\n"
        f"MemAvailable:   {available_kb} kB\n"
        f"Buffers:           500 kB\n"
    )


# ---------------------------------------------------------------------------
# S1 — alert fires when memory is below threshold
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fires_when_memory_below_threshold():
    """S1: MemAvailable/MemTotal = 10% < default 15% threshold → alert sent."""
    from src.scheduler.handlers.host_memory_alert import HostMemoryAlertHandler

    handler = HostMemoryAlertHandler()
    ctx = _make_ctx(threshold_pct=15)

    # 10 GB total, 1 GB available → 10% free (below 15% threshold)
    total_kb = 10 * 1024 * 1024
    available_kb = 1 * 1024 * 1024
    meminfo = _meminfo_content(total_kb, available_kb)

    sent: list[str] = []

    async def fake_tg_send(text: str, **_kwargs) -> bool:
        sent.append(text)
        return True

    with patch("builtins.open", MagicMock(return_value=MagicMock(
        __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=meminfo))),
        __exit__=MagicMock(return_value=False),
    ))):
        with patch("src.scheduler.handlers.host_memory_alert.telegram") as mock_tg:
            mock_tg.send = fake_tg_send
            await handler.run(ctx)

    assert len(sent) == 1, "Expected exactly one Telegram alert to be sent"
    assert "memory" in sent[0].lower() or "Memory" in sent[0], \
        "Alert message should mention memory"
    assert "10.0%" in sent[0] or "10%" in sent[0], \
        "Alert message should include the current free percentage"


# ---------------------------------------------------------------------------
# S2 — no alert when memory is healthy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_alert_when_memory_healthy():
    """S2: MemAvailable/MemTotal = 40% > 15% threshold → no alert sent."""
    from src.scheduler.handlers.host_memory_alert import HostMemoryAlertHandler

    handler = HostMemoryAlertHandler()
    ctx = _make_ctx(threshold_pct=15)

    # 10 GB total, 4 GB available → 40% free (above threshold)
    total_kb = 10 * 1024 * 1024
    available_kb = 4 * 1024 * 1024
    meminfo = _meminfo_content(total_kb, available_kb)

    sent: list[str] = []

    async def fake_tg_send(text: str, **_kwargs) -> bool:
        sent.append(text)
        return True

    with patch("builtins.open", MagicMock(return_value=MagicMock(
        __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=meminfo))),
        __exit__=MagicMock(return_value=False),
    ))):
        with patch("src.scheduler.handlers.host_memory_alert.telegram") as mock_tg:
            mock_tg.send = fake_tg_send
            await handler.run(ctx)

    assert len(sent) == 0, "No alert should be sent when memory is healthy"


# ---------------------------------------------------------------------------
# S3 — cooldown / dedup: should_run returns False within the interval
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cooldown_prevents_repeated_alerts():
    """S3: After an alert fires, should_run returns False until interval elapses."""
    from src.scheduler.handlers.host_memory_alert import HostMemoryAlertHandler

    # Use a 1-hour interval (3600 s)
    handler = HostMemoryAlertHandler()
    ctx = _make_ctx(interval_seconds=3600)

    # Simulate that an alert was just sent by setting _last_alert to now
    handler._last_alert = _time.monotonic()

    # should_run must return False immediately after an alert
    result = await handler.should_run(ctx)
    assert result is False, \
        "should_run must return False within the cooldown interval after an alert"


@pytest.mark.asyncio
async def test_should_run_true_on_first_call():
    """S3b: On a fresh handler (no prior alert), should_run returns True."""
    from src.scheduler.handlers.host_memory_alert import HostMemoryAlertHandler

    handler = HostMemoryAlertHandler()
    ctx = _make_ctx(interval_seconds=3600)

    result = await handler.should_run(ctx)
    assert result is True, \
        "should_run must return True on first call (no prior alert)"


@pytest.mark.asyncio
async def test_last_alert_only_updated_on_successful_send():
    """S3c: _last_alert is NOT updated when memory is healthy (no alert sent).

    Mirrors the semantics of BridgeUnpairedAlertHandler: the cooldown timer
    resets only after a successful alert send, not on every healthy probe.
    """
    from src.scheduler.handlers.host_memory_alert import HostMemoryAlertHandler

    handler = HostMemoryAlertHandler()
    ctx = _make_ctx(threshold_pct=15)

    initial_last_alert = handler._last_alert

    # Memory is healthy — no alert should fire
    total_kb = 10 * 1024 * 1024
    available_kb = 4 * 1024 * 1024
    meminfo = _meminfo_content(total_kb, available_kb)

    with patch("builtins.open", MagicMock(return_value=MagicMock(
        __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=meminfo))),
        __exit__=MagicMock(return_value=False),
    ))):
        with patch("src.scheduler.handlers.host_memory_alert.telegram") as mock_tg:
            mock_tg.send = AsyncMock(return_value=True)
            await handler.run(ctx)

    assert handler._last_alert == initial_last_alert, \
        "_last_alert must NOT be updated when no alert is sent (healthy memory)"
