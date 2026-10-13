import asyncio
from datetime import datetime, timedelta, timezone

from src.dashboard.api.surfaces import (
    FeedControlUpdate,
    collection_surface_progress,
    collector_operations,
    update_feed_control,
)


class _Conn:
    def __init__(self, *, ready=True):
        self.ready = ready

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def acquire(self):
        return self

    async def fetchval(self, query):
        if "COUNT(*)" in query:
            return 2
        return self.ready

    async def fetch(self, query, *args, **kwargs):
        now = datetime.now(timezone.utc)
        if "FROM browser_ingest_events" in query:
            if "latest_capture" in query:
                return [{
                    "platform": "x",
                    "surface": "following",
                    "last_capture_at": now - timedelta(days=10),
                    "last_success_at": now - timedelta(days=10),
                    "last_failure_at": None,
                }]
            return [{
                "platform": "x",
                "collection_surface": "for_you",
                "capture_count": 3,
                "observed_count": 12,
                "stored_count": 8,
                "deduped_count": 2,
                "failed_count": 2,
                "failed_captures": 1,
                "error_classes": [],
            }]
        if "FROM media_items" in query:
            if "latest_media" in query:
                return [{
                    "platform": "instagram",
                    "surface": "profile",
                    "last_media_at": now - timedelta(days=30),
                }]
            return [{
                "platform": "x",
                "collection_surface": "for_you",
                "media_rows": 5,
                "window_last_media_at": now - timedelta(minutes=3),
            }]
        if "AS messages" in query and "MAX(collected_at)" in query:
            return [{
                "platform": "telegram",
                "collection_surface": "message_stream",
                "last_message_at": now - timedelta(days=40),
            }]
        return [{
            "platform": "telegram",
            "collection_surface": "message_stream",
            "message_rows": 7,
            "last_message_at": now - timedelta(minutes=4),
        }]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self.conn


class _TimeoutFreshnessConn(_Conn):
    async def fetch(self, query, *args, **kwargs):
        if "latest_capture" in query:
            raise TimeoutError("bounded freshness lookup timed out")
        return await super().fetch(query, *args, **kwargs)


class _OpsConn:
    def __init__(self):
        self.executed = None

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def fetchval(self, query):
        return True

    async def fetch(self, query, *args, **kwargs):
        now = datetime.now(timezone.utc)
        if "FROM collection_schedules" in query:
            return [{
                "source": "x",
                "enabled": False,
                "interval_hours": 12,
                "next_run": now + timedelta(hours=12),
            }]
        if "FROM spider_queue" in query:
            return [{
                "platform": "tiktok",
                "status": "pending",
                "item_count": 2,
                "oldest_pending_at": now - timedelta(minutes=20),
                "oldest_in_progress_at": None,
            }]
        if "FROM browser_ingest_events" in query:
            return [{
                "platform": "x",
                "error_class": "HTTP 429",
                "error_count": 3,
                "last_seen_at": now,
            }]
        if "FROM spider_fetch_observations" in query:
            return [{
                "platform": "strava",
                "error_class": "ReadTimeout",
                "error_count": 1,
                "last_seen_at": now,
            }]
        if "FROM dead_letter_queue" in query:
            return [{
                "platform": "github",
                "error_class": "rate_limit",
                "error_count": 4,
                "last_seen_at": now,
            }]
        return []

    async def execute(self, query, *args):
        self.executed = (query, args)
        return "INSERT 0 1"


def test_surface_progress_groups_and_returns_capture_metrics(monkeypatch):
    import src.dashboard.api.surfaces as surfaces

    async def fake_pool():
        return _Pool(_Conn())

    monkeypatch.setattr(surfaces, "get_pool", fake_pool)
    result = asyncio.run(collection_surface_progress(hours=24, _user={"role": "viewer"}))
    assert result["available"] is True
    assert result["hours"] == 24
    row = next(item for item in result["surfaces"] if item["platform"] == "x")
    assert (row["platform"], row["surface"]) == ("x", "for_you")
    assert row["observed_count"] == 12
    assert row["stored_count"] == 8
    assert row["deduped_count"] == 2
    assert row["failed_count"] == 2
    assert row["media_rows"] == 5
    assert row["freshness_seconds"] >= 0
    stale_event = next(item for item in result["surfaces"] if item["surface"] == "following")
    assert stale_event["capture_count"] == 0
    assert stale_event["last_success_at"] is not None
    assert stale_event["freshness_seconds"] >= 10 * 24 * 60 * 60
    stale_media = next(item for item in result["surfaces"] if item["platform"] == "instagram")
    assert stale_media["media_rows"] == 0
    assert stale_media["freshness_seconds"] >= 30 * 24 * 60 * 60
    message = next(item for item in result["surfaces"] if item["platform"] == "telegram")
    assert message["platform"] == "telegram"
    assert message["surface"] == "message_stream"
    assert message["message_rows"] == 7
    assert message["capture_path"] == "message_stream"


def test_surface_progress_reports_pending_migration(monkeypatch):
    import src.dashboard.api.surfaces as surfaces

    async def fake_pool():
        return _Pool(_Conn(ready=False))

    monkeypatch.setattr(surfaces, "get_pool", fake_pool)
    result = asyncio.run(collection_surface_progress(hours=24, _user={"role": "viewer"}))
    assert result["available"] is False
    assert result["reason"] == "collection_surface_migration_pending"


def test_surface_progress_marks_timed_out_stale_lookup_incomplete(monkeypatch):
    import src.dashboard.api.surfaces as surfaces

    async def fake_pool():
        return _Pool(_TimeoutFreshnessConn())

    monkeypatch.setattr(surfaces, "get_pool", fake_pool)
    result = asyncio.run(collection_surface_progress(hours=24, _user={"role": "viewer"}))
    assert result["available"] is True
    assert result["freshness_available"] is False
    assert result["freshness_error_class"] == "timeout"


def test_collector_operations_reports_queue_age_errors_and_controls(monkeypatch):
    import src.dashboard.api.surfaces as surfaces

    async def fake_pool():
        return _Pool(_OpsConn())

    monkeypatch.setattr(surfaces, "get_pool", fake_pool)
    result = asyncio.run(collector_operations(hours=24, _user={"role": "viewer"}))
    assert result["available"] is True
    assert result["queue_age_available"] is True
    queue = next(row for row in result["queues"] if row["platform"] == "tiktok")
    assert queue["pending_count"] == 2
    assert queue["oldest_pending_age_seconds"] >= 0
    control = next(row for row in result["controls"] if row["platform"] == "x")
    assert control["enabled"] is False
    assert control["interval_hours"] == 12
    errors = {(row["platform"], row["category"]) for row in result["errors"]}
    assert ("x", "rate_limit") in errors
    assert ("strava", "timeout") in errors
    assert ("github", "rate_limit") in errors
    assert next(row for row in result["errors"] if row["source"] == "headless_dlq")["error_class"] == "rate_limit"
    assert result["error_reporting_available"] is True
    assert result["error_reporting_partial"] is False


class _FailingErrorOpsConn(_OpsConn):
    async def fetch(self, query, *args, **kwargs):
        if any(table in query for table in (
            "FROM browser_ingest_events", "FROM spider_fetch_observations", "FROM dead_letter_queue",
        )) and "error_count" in query:
            raise TimeoutError("database query timed out")
        return await super().fetch(query, *args, **kwargs)


def test_collector_operations_marks_failed_error_queries_unavailable(monkeypatch):
    import src.dashboard.api.surfaces as surfaces

    async def fake_pool():
        return _Pool(_FailingErrorOpsConn())

    monkeypatch.setattr(surfaces, "get_pool", fake_pool)
    result = asyncio.run(collector_operations(hours=24, _user={"role": "viewer"}))
    assert result["errors"] == []
    assert result["error_reporting_available"] is False
    assert result["error_reporting_partial"] is True
    assert all(not row["available"] for row in result["error_sources"])
    assert all(row["error_class"] == "timeout" for row in result["error_sources"])


def test_feed_control_update_is_persisted_and_bounded(monkeypatch):
    import src.dashboard.api.surfaces as surfaces
    from pydantic import ValidationError

    conn = _OpsConn()

    async def fake_pool():
        return _Pool(conn)

    monkeypatch.setattr(surfaces, "get_pool", fake_pool)
    result = asyncio.run(update_feed_control(
        "x", FeedControlUpdate(enabled=False, interval_hours=6), _user={"role": "operator"},
    ))
    assert result["platform"] == "x"
    assert result["enabled"] is False
    assert result["interval_hours"] == 6
    assert conn.executed[1][0] == "x"
    assert conn.executed[1][1] == 6
    assert conn.executed[1][2] is False
    try:
        FeedControlUpdate(enabled=True, interval_hours=721)
    except ValidationError:
        pass
    else:
        raise AssertionError("interval above 720 hours must be rejected")
