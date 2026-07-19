"""Shared jitter helpers for collector request pacing."""
from __future__ import annotations

import asyncio
import logging
import os
import random

logger = logging.getLogger(__name__)

_FALSE = {"0", "false", "no", "off"}


def _enabled(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSE


def _float_env(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def jittered_delay(seconds: float) -> float:
    """Return a non-deterministic delay for collector rate-limit sleeps."""
    base = max(0.0, float(seconds or 0.0))
    if base <= 0:
        return 0.0

    jitter = _float_env("COLLECTOR_RATE_LIMIT_JITTER", 0.45, minimum=0.0, maximum=3.0)
    factor = random.uniform(max(0.10, 1.0 - jitter), 1.0 + jitter)
    delay = base * factor

    pause_prob = _float_env(
        "COLLECTOR_EXTRA_PAUSE_PROBABILITY",
        0.08,
        minimum=0.0,
        maximum=1.0,
    )
    if random.random() < pause_prob:
        lo = _float_env("COLLECTOR_EXTRA_PAUSE_MIN", 2.0, minimum=0.0)
        hi = _float_env("COLLECTOR_EXTRA_PAUSE_MAX", 12.0, minimum=lo)
        delay += random.uniform(lo, hi)

    return max(0.0, delay)


async def sleep_rate_limit(seconds: float) -> None:
    delay = jittered_delay(seconds)
    if delay > 0:
        await asyncio.sleep(delay)


async def headless_dwell(label: str = "") -> None:
    """Small random dwell for Playwright/headless browser navigation paths."""
    if not _enabled("HEADLESS_NAV_DWELL_ENABLED", True):
        return

    lo = _float_env("HEADLESS_NAV_DWELL_MIN", 1.0, minimum=0.0)
    hi = _float_env("HEADLESS_NAV_DWELL_MAX", 4.0, minimum=lo)
    if hi <= 0:
        return

    delay = random.uniform(lo, hi)
    long_prob = _float_env(
        "HEADLESS_NAV_LONG_PAUSE_PROBABILITY",
        0.05,
        minimum=0.0,
        maximum=1.0,
    )
    if random.random() < long_prob:
        extra_lo = _float_env("HEADLESS_NAV_LONG_PAUSE_MIN", 8.0, minimum=0.0)
        extra_hi = _float_env("HEADLESS_NAV_LONG_PAUSE_MAX", 25.0, minimum=extra_lo)
        delay += random.uniform(extra_lo, extra_hi)

    if label:
        logger.debug("headless dwell %.2fs before %s", delay, label)
    await asyncio.sleep(delay)
