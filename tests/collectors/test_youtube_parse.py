"""Unit tests for youtube pure parse helpers (STAGE 2 safety net)."""
from datetime import datetime, timezone

from src.collectors.youtube.parse import vtt_to_text, parse_relative_timestamp


def test_vtt_strips_headers_timing_and_tags():
    vtt = (
        "WEBVTT\n"
        "Kind: captions\n"
        "Language: en\n\n"
        "00:00:01.000 --> 00:00:02.000\n"
        "Hello <c>world</c>\n"
        "Hello world\n\n"
        "2\n"
        "Second line\n"
    )
    assert vtt_to_text(vtt) == "Hello world\nSecond line"


def test_vtt_empty():
    assert vtt_to_text("") == ""
    assert vtt_to_text("WEBVTT\n\n00:00 --> 00:01\n") == ""


def test_relative_timestamp_valid():
    assert parse_relative_timestamp("3 days ago") is not None
    assert isinstance(parse_relative_timestamp("1 month ago (edited)"), datetime)
    # roughly 1 hour ago
    out = parse_relative_timestamp("1 hour ago")
    now = datetime.now(timezone.utc)
    assert 3500 < (now - out).total_seconds() < 3700


def test_relative_timestamp_invalid():
    assert parse_relative_timestamp("") is None
    assert parse_relative_timestamp("garbage") is None
    assert parse_relative_timestamp("just now") is None
