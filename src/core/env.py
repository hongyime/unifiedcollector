"""Typed environment-variable helpers (P2-1).

The codebase had ~36 boolean env checks written 7 different ways:
    os.getenv("X", "true").lower() == "true"
    os.getenv("X", "0") == "1"
    os.getenv("X", "").lower() in ("1", "true", "yes", "on")

Inconsistent parsing is a latent bug source: ``X=TRUE``, ``X=yes``, ``X=1`` all
mean "on" to a human but several of the idioms above only accept one spelling,
so a perfectly reasonable value silently parses as False. These helpers give one
canonical truthy set and fail loudly on malformed required values.

Usage:
    from src.core.env import env_bool, env_int, env_float, env_str
    if env_bool("BEEPER_COLLECTOR_ENABLED", default=False):
        ...
    batch = env_int("TELEGRAM_BACKFILL_BATCH_SIZE", default=100, min_value=1)
"""
from __future__ import annotations

import os

# Canonical truthy / falsy spellings (case-insensitive).
_TRUE = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE = frozenset({"0", "false", "no", "off", "n", "f", ""})


def env_bool(key: str, default: bool = False) -> bool:
    """Parse an env var as a boolean using the canonical truthy set.

    Unset -> ``default``. A set-but-unrecognised value raises ValueError so a
    typo (``X=ture``) surfaces at startup instead of silently disabling a
    collector.
    """
    raw = os.getenv(key)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    raise ValueError(
        f"env var {key}={raw!r} is not a recognised boolean "
        f"(use one of: 1/0, true/false, yes/no, on/off)"
    )


def env_int(key: str, default: int, *, min_value: int | None = None,
            max_value: int | None = None) -> int:
    """Parse an env var as an int with optional bounds. Unset -> default."""
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"env var {key}={raw!r} is not an integer") from exc
    if min_value is not None and val < min_value:
        raise ValueError(f"env var {key}={val} below minimum {min_value}")
    if max_value is not None and val > max_value:
        raise ValueError(f"env var {key}={val} above maximum {max_value}")
    return val


def env_float(key: str, default: float, *, min_value: float | None = None,
              max_value: float | None = None) -> float:
    """Parse an env var as a float with optional bounds. Unset -> default."""
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"env var {key}={raw!r} is not a number") from exc
    if min_value is not None and val < min_value:
        raise ValueError(f"env var {key}={val} below minimum {min_value}")
    if max_value is not None and val > max_value:
        raise ValueError(f"env var {key}={val} above maximum {max_value}")
    return val


def env_str(key: str, default: str | None = None, *, required: bool = False) -> str | None:
    """Read a string env var. ``required=True`` raises if unset/empty."""
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        if required:
            raise ValueError(f"required env var {key} is not set")
        return default
    return raw
