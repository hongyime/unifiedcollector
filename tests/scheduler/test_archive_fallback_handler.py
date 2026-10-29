"""Tests for ArchiveFallbackHandler — scheduler cadence + enabled gate."""
from __future__ import annotations

import time as _time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _make_ctx(*, enabled: int = 1, interval_minutes: int = 360):
    mapping = {
        "ARCHIVE_FALLBACK_ENABLED": enabled,
        "ARCHIVE_FALLBACK_INTERVAL_MINUTES": interval_minutes,
    }

    def _get_env_int(name: str, default: int, *, min_value: int = 0) -> int:
        return max(min_value, mapping.get(name, default))

    return SimpleNamespace(pool=object(), get_env_int=_get_env_int)


@pytest.mark.asyncio
async def test_should_run_false_when_disabled():
    from src.scheduler.handlers.archive_fallback import ArchiveFallbackHandler

    handler = ArchiveFallbackHandler()
    assert await handler.should_run(_make_ctx(enabled=0)) is False


@pytest.mark.asyncio
async def test_should_run_false_when_not_due():
    from src.scheduler.handlers.archive_fallback import ArchiveFallbackHandler

    handler = ArchiveFallbackHandler()
    handler._last_run = _time.monotonic()
    assert await handler.should_run(_make_ctx(interval_minutes=360)) is False


@pytest.mark.asyncio
async def test_should_run_true_when_due():
    from src.scheduler.handlers.archive_fallback import ArchiveFallbackHandler

    handler = ArchiveFallbackHandler()
    # _last_run defaults to 0.0, so the interval has always elapsed.
    assert await handler.should_run(_make_ctx()) is True


@pytest.mark.asyncio
async def test_run_calls_pipeline_with_pool():
    from src.scheduler.handlers.archive_fallback import ArchiveFallbackHandler

    handler = ArchiveFallbackHandler()
    ctx = _make_ctx()
    with patch(
        "src.scheduler.handlers.archive_fallback.run_archive_fallback",
        new=AsyncMock(),
    ) as run:
        await handler.run(ctx)
    run.assert_awaited_once_with(ctx.pool)


def test_handler_registered():
    from src.scheduler.handlers import HANDLERS
    from src.scheduler.handlers.archive_fallback import ArchiveFallbackHandler

    assert any(isinstance(h, ArchiveFallbackHandler) for h in HANDLERS)
