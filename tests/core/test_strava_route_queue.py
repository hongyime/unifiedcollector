import asyncio
from datetime import datetime, timezone

from src.core.strava_route_queue import fetch_strava_route_capture_queue


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    def __init__(self, *, cooldown=None, rows=None):
        self.cooldown = cooldown
        self.rows = rows or []
        self.fetch_called = False
        self.fetch_args = None

    async def fetchrow(self, *_):
        return self.cooldown

    async def fetch(self, _query, *args):
        self.fetch_called = True
        self.fetch_args = args
        return self.rows


def test_route_capture_queue_respects_active_gps_cooldown():
    conn = _Conn(
        cooldown={
            "cooldown_until": datetime(2026, 7, 23, 13, 0, tzinfo=timezone.utc),
            "reason": "streams 429",
        }
    )

    out = asyncio.run(
        fetch_strava_route_capture_queue(_Pool(conn), limit=3, respect_cooldown=True)
    )

    assert out["items"] == []
    assert out["cooldown"]["active"] is True
    assert out["cooldown"]["until"] == "2026-07-23T13:00:00+00:00"
    assert conn.fetch_called is False


def test_route_capture_queue_normalizes_rows_when_not_cooling_down():
    conn = _Conn(
        rows=[
            {
                "platform_activity_id": 19283135496,
                "name": "Morning Run",
                "type": "Run",
                "sport_type": "Run",
                "start_date": datetime(2026, 7, 22, 1, 2, 3, tzinfo=timezone.utc),
                "start_latlng": "[1.3,103.8]",
                "stream_status": None,
                "platform_athlete_id": 72101656,
                "athlete_name": "Me",
                "proximity_tier": 1,
                "target_priority": 95,
                "last_browser_visit_at": None,
            }
        ]
    )

    out = asyncio.run(
        fetch_strava_route_capture_queue(_Pool(conn), limit=2, respect_cooldown=False)
    )

    assert conn.fetch_args == (2, 6)
    assert out["cooldown"]["active"] is False
    assert out["items"] == [
        {
            "platform_activity_id": 19283135496,
            "activity_url": "https://www.strava.com/activities/19283135496",
            "name": "Morning Run",
            "type": "Run",
            "sport_type": "Run",
            "start_date": "2026-07-22T01:02:03+00:00",
            "start_latlng": "[1.3,103.8]",
            "stream_status": None,
            "platform_athlete_id": 72101656,
            "athlete_name": "Me",
            "proximity_tier": 1,
            "target_priority": 95,
            "last_browser_visit_at": None,
        }
    ]
