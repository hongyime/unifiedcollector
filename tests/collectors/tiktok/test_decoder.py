"""Tests for src.collectors.tiktok.decoder."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from src.collectors.tiktok.decoder import TIKTOK_EPOCH_UNIX, tiktok_id_to_utc


def _make_id(unix_ts: int, low_bits: int = 0) -> int:
    """Build a valid-shaped TikTok ID from a target UNIX timestamp."""
    seconds_since_epoch = unix_ts - TIKTOK_EPOCH_UNIX
    return (seconds_since_epoch << 32) | (low_bits & 0xFFFFFFFF)


def test_epoch_boundary():
    # An ID whose high 32 bits are 0 → seconds_since_epoch is 0 → invalid.
    assert tiktok_id_to_utc(0) is None


def test_none_input():
    assert tiktok_id_to_utc(None) is None


def test_negative_input():
    assert tiktok_id_to_utc(-1) is None


def test_string_of_digits_accepted():
    target = TIKTOK_EPOCH_UNIX + 86400  # 2016-06-02 00:00:00 UTC
    pid = str(_make_id(target))
    got = tiktok_id_to_utc(pid)
    assert got is not None
    assert got == datetime(2016, 6, 2, 0, 0, 0, tzinfo=timezone.utc)


def test_non_numeric_string_returns_none():
    assert tiktok_id_to_utc("not-a-number") is None
    assert tiktok_id_to_utc("") is None
    assert tiktok_id_to_utc("   ") is None


def test_known_calibration_date():
    # 2023-01-01 00:00:00 UTC as a known post creation time.
    target_dt = datetime(2023, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    pid = _make_id(int(target_dt.timestamp()))
    got = tiktok_id_to_utc(pid)
    assert got == target_dt


def test_short_id_returns_none():
    # A very short numeric string decodes to seconds_since_epoch=0.
    assert tiktok_id_to_utc(12345) is None


def test_far_future_returns_none():
    # ID that would decode to > 1 year in the future should be rejected
    # (clock-skew / corruption guard).
    now = int(time.time())
    future_ts = now + 86400 * 400  # 400 days in the future
    pid = _make_id(future_ts)
    assert tiktok_id_to_utc(pid) is None


def test_recent_id_decodes_within_a_minute_of_now():
    now = int(time.time())
    pid = _make_id(now)
    got = tiktok_id_to_utc(pid)
    assert got is not None
    diff = abs((got - datetime.fromtimestamp(now, tz=timezone.utc)).total_seconds())
    assert diff < 60
