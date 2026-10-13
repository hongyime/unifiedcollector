import asyncio
from datetime import datetime, timedelta, timezone

from src.dashboard.api.surfaces import collection_surface_progress


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

    async def fetch(self, query, hours):
        now = datetime.now(timezone.utc)
        if "FROM browser_ingest_events" in query:
            return [{
                "platform": "x",
                "collection_surface": "for_you",
                "capture_count": 3,
                "observed_count": 12,
                "stored_count": 8,
                "deduped_count": 2,
                "failed_count": 2,
                "failed_captures": 1,
                "last_capture_at": now,
                "last_success_at": now - timedelta(minutes=2),
                "last_failure_at": now - timedelta(minutes=1),
            }]
        if "FROM media_items" in query:
            return [{
                "platform": "x",
                "collection_surface": "for_you",
                "media_rows": 5,
                "last_media_at": now - timedelta(minutes=3),
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
