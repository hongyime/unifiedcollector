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
    def __init__(self, *, cooldown=None, cooldowns=None, rows=None):
        if cooldowns is None and cooldown is not None:
            cooldowns = [cooldown]
        self.cooldowns = cooldowns or []
        self.rows = rows or []
        self.fetchrow_query = None
        self.fetchrow_args = None
        self.fetch_query = None
        self.fetch_called = False
        self.fetch_args = None

    async def fetchrow(self, _query, *args):
        self.fetchrow_query = _query
        self.fetchrow_args = args
        account = args[0] if args else None
        for row in self.cooldowns:
            row_account = row.get("account")
            if not row_account or (account and row_account == account):
                return row
        return None

    async def fetch(self, _query, *args):
        self.fetch_called = True
        self.fetch_query = _query
        self.fetch_args = args
        return self.rows


def test_route_capture_queue_respects_active_gps_cooldown():
    conn = _Conn(
        cooldown={
            "cooldown_until": datetime(2026, 7, 23, 13, 0, tzinfo=timezone.utc),
            "reason": "streams 429",
            "account": None,
            "scope": "gps_streams",
        }
    )

    out = asyncio.run(
        fetch_strava_route_capture_queue(_Pool(conn), limit=3, respect_cooldown=True)
    )

    assert out["items"] == []
    assert out["cooldown"]["active"] is True
    assert out["cooldown"]["until"] == "2026-07-23T13:00:00+00:00"
    assert out["cooldown"]["scope"] == "gps_streams"
    assert conn.fetch_called is False
    assert "browser_ingest_events bie" in conn.fetchrow_query
    assert "strava_gps_streams s" in conn.fetchrow_query


def test_route_capture_queue_respects_matching_account_cooldown():
    conn = _Conn(
        cooldown={
            "cooldown_until": datetime(2026, 7, 23, 13, 0, tzinfo=timezone.utc),
            "reason": "browser Strava stream HTTP 429",
            "account": "bryanseah234",
            "scope": "browser_strava_streams",
        }
    )

    out = asyncio.run(
        fetch_strava_route_capture_queue(
            _Pool(conn),
            limit=3,
            account="bryanseah234",
            respect_cooldown=True,
        )
    )

    assert conn.fetchrow_args == ("bryanseah234",)
    assert out["items"] == []
    assert out["cooldown"]["active"] is True
    assert out["cooldown"]["account"] == "bryanseah234"
    assert out["cooldown"]["scope"] == "browser_strava_streams"
    assert conn.fetch_called is False


def test_route_capture_queue_ignores_other_account_cooldown():
    conn = _Conn(
        cooldown={
            "cooldown_until": datetime(2026, 7, 23, 13, 0, tzinfo=timezone.utc),
            "reason": "browser Strava stream HTTP 429",
            "account": "bryanseah234",
            "scope": "browser_strava_streams",
        },
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
        ],
    )

    out = asyncio.run(
        fetch_strava_route_capture_queue(
            _Pool(conn),
            limit=2,
            account="shotsbyseah234",
            respect_cooldown=True,
        )
    )

    assert conn.fetchrow_args == ("shotsbyseah234",)
    assert conn.fetch_called is True
    assert out["cooldown"]["active"] is False
    assert out["account"] == "shotsbyseah234"
    assert [item["platform_activity_id"] for item in out["items"]] == [19283135496]


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

    assert conn.fetch_args == (2, 6, 300, None, 10000)
    assert out["cooldown"]["active"] is False
    assert out["recent_candidate_limit"] == 300
    assert out["important_candidate_limit"] == 10000
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


def test_route_capture_queue_keeps_important_candidates_outside_recent_cap():
    conn = _Conn(rows=[])

    asyncio.run(
        fetch_strava_route_capture_queue(
            _Pool(conn),
            limit=2,
            account="72101656",
            respect_cooldown=False,
        )
    )

    assert conn.fetch_args == (2, 6, 300, "72101656", 10000)
    assert "important_candidates AS MATERIALIZED" in conn.fetch_query
    assert "ap.tier <= 2" in conn.fetch_query
    assert "recent_candidates AS MATERIALIZED" in conn.fetch_query
