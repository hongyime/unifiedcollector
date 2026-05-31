"""Unit tests for strava pure normalize helpers (STAGE 2 safety net)."""
from src.collectors.strava.parse import (
    normalize_training_activity,
    normalize_feed_activity,
)


def test_training_numeric_passthrough():
    out = normalize_training_activity({
        "id": 99, "name": "Morning Run", "type": "Run",
        "distance": 10000, "moving_time": 3600, "elapsed_time": 3700,
        "total_elevation_gain": 120, "start_date": "2026-04-15T06:00:00Z",
    })
    assert out["id"] == 99
    assert out["distance"] == 10000
    assert out["moving_time"] == 3600
    assert out["type"] == "Run"


def test_training_unit_strings_parsed():
    out = normalize_training_activity({
        "id": 1, "name": "x", "type": "Ride",
        "distance": "9.99mi", "moving_time": "1h 2m 3s", "elapsed_time": "62:30",
    })
    assert out["distance"] == 9.99
    assert out["moving_time"] == 3723           # 1h2m3s
    assert out["elapsed_time"] == 62 * 60 + 30  # mm:ss


def test_training_human_date_to_iso():
    out = normalize_training_activity({
        "id": 2, "name": "y", "type": "Run",
        "start_date_local": "Wed, 4/15/2026",
    })
    assert out["start_date"].startswith("2026-04-15")


def test_feed_nested_activity():
    out = normalize_feed_activity({
        "activity": {"id": 555, "name": "Evening", "type": "Run",
                     "distance": 5000, "start_date": "2026-04-15T18:00:00Z"},
        "athlete": {"id": 7, "name": "Bryan"},
    })
    assert out["id"] == 555
    assert out["_athlete_id"] == 7
    assert out["_source"] == "following_feed"
    assert out["_athlete_name"] == "Bryan"


def test_feed_missing_id_returns_none():
    assert normalize_feed_activity({"activity": {"name": "no id"}}) is None
    assert normalize_feed_activity("notadict") is None
    assert normalize_feed_activity({"activity": {"id": 1}}) is None  # no start_date
