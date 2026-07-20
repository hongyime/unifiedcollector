from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

from src.dashboard.api import _strava_route_status


def test_strava_route_status_mapped_when_polyline_present():
    out = _strava_route_status({"summary_polyline": "abc"})

    assert out["route_status"] == "mapped"


def test_strava_route_status_active_429_cooldown():
    out = _strava_route_status({
        "gps_rate_limit_until": datetime.now(timezone.utc) + timedelta(minutes=10),
        "gps_rate_limit_reason": "streams 429 for 123 via page",
    })

    assert out["route_status"] == "rate_limited"
    assert "429" in (out["route_status_detail"] or "")


def test_strava_route_status_stays_queued_before_definitive_result():
    out = _strava_route_status({"stream_status": None})

    assert out["route_status"] == "queued"


def test_strava_route_status_privacy_zone():
    out = _strava_route_status({"stream_status": "truncated_empty"})

    assert out["route_status"] == "privacy_zone"
