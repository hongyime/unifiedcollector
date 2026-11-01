"""Tests for BrowserRotationHandler — scheduler cadence + rotator invocation."""
from __future__ import annotations

import time as _time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _make_ctx(*, interval_minutes: int = 30):
    mapping = {"COLLECTOR_BROWSER_ROTATION_INTERVAL_MINUTES": interval_minutes}

    def _get_env_int(name: str, default: int, *, min_value: int = 0) -> int:
        return max(min_value, mapping.get(name, default))

    return SimpleNamespace(pool=object(), get_env_int=_get_env_int)


@pytest.mark.asyncio
async def test_should_run_false_when_not_due():
    from src.scheduler.handlers.browser_rotation import BrowserRotationHandler

    handler = BrowserRotationHandler()
    handler._last_run = _time.monotonic()
    assert await handler.should_run(_make_ctx(interval_minutes=30)) is False


@pytest.mark.asyncio
async def test_should_run_true_when_due():
    from src.scheduler.handlers.browser_rotation import BrowserRotationHandler

    handler = BrowserRotationHandler()
    # _last_run defaults to 0.0, so the interval has always elapsed.
    assert await handler.should_run(_make_ctx()) is True


@pytest.mark.asyncio
async def test_run_calls_rotator_with_pool():
    from src.scheduler.handlers.browser_rotation import BrowserRotationHandler

    handler = BrowserRotationHandler()
    ctx = _make_ctx()
    with patch(
        "src.scheduler.handlers.browser_rotation.rotate_browser_schedules",
        new=AsyncMock(),
    ) as rotate:
        await handler.run(ctx)
    rotate.assert_awaited_once_with(ctx.pool)


@pytest.mark.asyncio
async def test_run_swallows_rotator_exception():
    from src.scheduler.handlers.browser_rotation import BrowserRotationHandler

    handler = BrowserRotationHandler()
    with patch(
        "src.scheduler.handlers.browser_rotation.rotate_browser_schedules",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await handler.run(_make_ctx())  # must not raise


def test_handler_registered():
    from src.scheduler.handlers import HANDLERS
    from src.scheduler.handlers.browser_rotation import BrowserRotationHandler

    assert any(isinstance(h, BrowserRotationHandler) for h in HANDLERS)
