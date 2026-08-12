from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

import pytest

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

from src.dashboard import api as dashboard_api  # noqa: E402


class _Acquire:
    def __init__(self, conn: "_FakeConn"):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return None


class _Pool:
    def __init__(self, conn: "_FakeConn"):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.fetch_calls = 0

    async def fetch(self, *_args):
        self.fetch_calls += 1
        return self.rows


def _row(source: str, status: str, created_at: datetime):
    return {
        "source": source,
        "expected_cadence": "24:00:00",
        "latest_data_at": created_at,
        "latest_run_at": created_at,
        "status": status,
        "rows_24h": 10,
        "media_24h": 4,
        "errors_24h": 0,
        "rate_limits_24h": 0,
        "private_access_failures": 0,
        "stale_targets": [],
        "created_at": created_at,
    }


@pytest.mark.asyncio
async def test_collectors_coverage_refreshes_stale_snapshot(monkeypatch):
    stale_time = datetime.now(timezone.utc) - timedelta(hours=3)
    fresh_time = datetime.now(timezone.utc)
    conn = _FakeConn([_row("x", "stale", stale_time)])
    calls = {"refresh": 0}

    async def fake_get_pool():
        return _Pool(conn)

    async def fake_refresh(refresh_conn):
        assert refresh_conn is conn
        calls["refresh"] += 1
        conn.rows = [_row("x", "fresh", fresh_time)]
        return {"written": 1}

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)
    monkeypatch.setattr(dashboard_api, "build_collection_coverage_snapshot", fake_refresh)
    monkeypatch.setattr(dashboard_api, "_COVERAGE_SNAPSHOT_STALE_SECONDS", 3600)

    result = await dashboard_api.collectors_coverage(_user={})

    assert calls["refresh"] == 1
    assert conn.fetch_calls == 2
    assert result["summary"]["fresh"] == 1
    assert result["snapshot_stale"] is False
    assert result["refresh_attempted"] is True


@pytest.mark.asyncio
async def test_collectors_coverage_reports_snapshot_age_without_refresh(monkeypatch):
    fresh_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    conn = _FakeConn([_row("telegram", "fresh", fresh_time)])

    async def fake_get_pool():
        return _Pool(conn)

    async def fake_refresh(_conn):
        raise AssertionError("fresh snapshots should not refresh")

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)
    monkeypatch.setattr(dashboard_api, "build_collection_coverage_snapshot", fake_refresh)
    monkeypatch.setattr(dashboard_api, "_COVERAGE_SNAPSHOT_STALE_SECONDS", 3600)

    result = await dashboard_api.collectors_coverage(_user={})

    assert conn.fetch_calls == 1
    assert result["total"] == 1
    assert result["summary"]["fresh"] == 1
    assert result["snapshot_age_seconds"] >= 0
    assert result["refresh_attempted"] is False


@pytest.mark.asyncio
async def test_media_realtime_feed_status_returns_safe_counts(monkeypatch):
    async def fake_status():
        return {
            "available": True,
            "queue_depth": 2,
            "failed_depth": 0,
            "local_fallback_total": 3,
            "local_fallback_by_source": {"youtube": 2, "telegram": 1},
            "local_fallback_last": {"source": "youtube", "target_name": "big.mp4"},
        }

    monkeypatch.setattr(dashboard_api, "_realtime_feed_status_from_redis", fake_status)

    result = await dashboard_api.media_realtime_feed_status(_user={})

    assert result["local_fallback_total"] == 3
    assert result["local_fallback_by_source"]["youtube"] == 2
    assert result["local_fallback_last"]["target_name"] == "big.mp4"
