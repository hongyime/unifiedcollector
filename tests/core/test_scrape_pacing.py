from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.core import scrape_pacing


def _deterministic_retry_env(monkeypatch) -> None:
    monkeypatch.setenv("COLLECTOR_PRE_COOLDOWN_RETRY_ENABLED", "true")
    monkeypatch.setenv("COLLECTOR_PRE_COOLDOWN_RETRY_BASE_SECONDS", "10")
    monkeypatch.setenv("COLLECTOR_PRE_COOLDOWN_RETRY_MAX_SECONDS", "25")
    monkeypatch.setenv("COLLECTOR_PRE_COOLDOWN_RETRY_MULTIPLIER", "2")
    monkeypatch.setenv("COLLECTOR_RATE_LIMIT_JITTER", "0")
    monkeypatch.setenv("COLLECTOR_EXTRA_PAUSE_PROBABILITY", "0")


def test_pre_cooldown_retry_delay_is_env_tunable(monkeypatch):
    _deterministic_retry_env(monkeypatch)

    assert scrape_pacing.pre_cooldown_retry_delay(1) == pytest.approx(10.0)
    assert scrape_pacing.pre_cooldown_retry_delay(2) == pytest.approx(20.0)
    assert scrape_pacing.pre_cooldown_retry_delay(3) == pytest.approx(25.0)


def test_pre_cooldown_retry_delay_disabled(monkeypatch):
    monkeypatch.setenv("COLLECTOR_PRE_COOLDOWN_RETRY_ENABLED", "0")

    assert scrape_pacing.pre_cooldown_retry_delay() == -1.0


@pytest.mark.asyncio
async def test_sleep_before_pre_cooldown_retry_uses_asyncio_sleep(monkeypatch):
    _deterministic_retry_env(monkeypatch)
    sleeper = AsyncMock()
    monkeypatch.setattr(scrape_pacing.asyncio, "sleep", sleeper)

    delay = await scrape_pacing.sleep_before_pre_cooldown_retry(
        "instagram",
        "profile_fetch",
        account="acct1",
    )

    assert delay == pytest.approx(10.0)
    sleeper.assert_awaited_once_with(10.0)


@pytest.mark.asyncio
async def test_sleep_before_pre_cooldown_retry_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("COLLECTOR_PRE_COOLDOWN_RETRY_ENABLED", "false")
    sleeper = AsyncMock()
    monkeypatch.setattr(scrape_pacing.asyncio, "sleep", sleeper)

    delay = await scrape_pacing.sleep_before_pre_cooldown_retry("strava", "gps_streams")

    assert delay is None
    sleeper.assert_not_called()
