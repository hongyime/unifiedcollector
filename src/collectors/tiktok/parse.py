"""Pure parsing/coercion helpers for the tiktok collector.

Extracted from the collector class (STAGE 2 of the per-package refactor). These
are side-effect-free functions — no ``self``, no I/O — so they are trivially
unit-testable and carry zero deploy risk. The collector keeps thin staticmethod
shims that delegate here for back-compat with existing call sites.
"""
from __future__ import annotations

from datetime import datetime, timezone


def safe_int(value, default: int = 0) -> int:
    """Coerce a value to int, returning ``default`` on None/empty/garbage."""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (ValueError, TypeError):
        return default


def to_dt(value):
    """Coerce a unix timestamp (int or str) to an aware UTC datetime.

    Returns None on None/empty/non-positive/garbage input.
    """
    if value is None or value == "":
        return None
    try:
        ts = int(value)
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None
