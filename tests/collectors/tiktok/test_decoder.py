"""Tests for src.collectors.tiktok.decoder."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from src.collectors.tiktok.decoder import tiktok_id_to_utc


def _make_id(unix_ts: int, low_bits: int = 0) -> int:
    """Build a valid-shaped TikTok ID from a target UNIX timestamp.

    High 32 bits = unix timestamp directly (verified against 10 live rows
    2026-09-30). Low 32 bits are the machine/sequence portion; any value.
    """
    return (unix_ts << 32) | (low_bits & 0xFFFFFFFF)


def test_epoch_boundary():
    # An ID whose high 32 bits are 0 -> unix_ts=0 -> invalid.
    assert tiktok_id_to_utc(0) is None


def test_none_input():
    assert tiktok_id_to_utc(None) is None


def test_negative_input():
    assert tiktok_id_to_utc(-1) is None


def test_string_of_digits_accepted():
    target_dt = datetime(2020, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    pid = str(_make_id(int(target_dt.timestamp())))
    got = tiktok_id_to_utc(pid)
    assert got is not None
    assert got == target_dt


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
    # A very short numeric string decodes to unix_ts=0.
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


def test_real_id_from_2021():
    # Real ID captured 2026-09-30 from tiktok_posts diagnostic run.
    # create_time in the DB: 2021-10-13 11:37:47 UTC.
    got = tiktok_id_to_utc("7018513720435395842")
    assert got is not None
    expected = datetime(2021, 10, 13, 11, 37, 47, tzinfo=timezone.utc)
    diff = abs((got - expected).total_seconds())
    assert diff < 10, f"decoded {got}, expected ~{expected}"


def test_pre_tiktok_returns_none():
    # Timestamps before 2016 should be rejected (TikTok didn't exist).
    old_pid = _make_id(1_000_000_000)  # 2001-09-09
    assert tiktok_id_to_utc(old_pid) is None
