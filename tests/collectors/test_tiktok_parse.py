"""Unit tests for tiktok pure parse helpers (STAGE 2 safety net)."""
from datetime import datetime, timezone

from src.collectors.tiktok.parse import safe_int, to_dt


def test_safe_int_valid():
    assert safe_int("5") == 5
    assert safe_int(7) == 7


def test_safe_int_garbage_returns_default():
    assert safe_int(None) == 0
    assert safe_int("") == 0
    assert safe_int("notanint") == 0
    assert safe_int("x", 9) == 9


def test_to_dt_valid():
    assert to_dt(1700000000) == datetime.fromtimestamp(1700000000, tz=timezone.utc)
    assert to_dt("1700000000") == datetime.fromtimestamp(1700000000, tz=timezone.utc)


def test_to_dt_garbage_returns_none():
    assert to_dt(None) is None
    assert to_dt("") is None
    assert to_dt(0) is None
    assert to_dt(-5) is None
    assert to_dt("notats") is None
