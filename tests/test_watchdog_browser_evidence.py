"""Unknown observations must not mutate the last known capture health."""

from unittest.mock import AsyncMock

import asyncpg
import pytest

from src.core import source_freshness


@pytest.mark.parametrize("heartbeat_age", [None, 20])
async def test_unknown_capture_evidence_neither_alarms_nor_clears_health(
    monkeypatch: pytest.MonkeyPatch, heartbeat_age: int | None,
) -> None:
    # Given timed-out freshness checks, optionally with a responding extension.
    monkeypatch.setenv("DATABASE_URL", "postgres://test:test@localhost/test")
    from src.watchdog import freshness

    monkeypatch.setattr(freshness, "BROWSER_SOURCE_WATCH_SOURCES", {"x"})
    monkeypatch.setattr(freshness, "_rotator_paused_sources", AsyncMock(return_value=set()))
    monkeypatch.setattr(source_freshness, "compute_liveness", AsyncMock(return_value=[{
        "source": "x", "status": "unknown", "age_seconds": None,
        "browser_heartbeat_age_seconds": heartbeat_age,
        "browser_content_stale": False, "detail": "no freshness row could be read",
    }]))
    degraded = AsyncMock()
    recovered_browser = AsyncMock()
    recovered_native = AsyncMock()
    notify = AsyncMock()
    monkeypatch.setattr(freshness, "_mark_degraded_browser_source", degraded)
    monkeypatch.setattr(freshness, "_mark_running_if_browser_watchdog", recovered_browser)
    monkeypatch.setattr(freshness, "_mark_running_if_stale_watchdog", recovered_native)
    monkeypatch.setattr(freshness, "_notify", notify)

    # When the watchdog consumes the unknown observation.
    await freshness._browser_source_tick(AsyncMock(spec=asyncpg.Connection))

    # Then it neither manufactures a stall nor a recovery.
    degraded.assert_not_awaited()
    recovered_browser.assert_not_awaited()
    recovered_native.assert_not_awaited()
    notify.assert_not_awaited()
