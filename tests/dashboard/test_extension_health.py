from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import time

import pytest

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

from src.dashboard import api as dashboard_api
from src.dashboard.api import (
    _browser_extension_fallback_payload_with_fast_ingest,
    _browser_extension_fallback_payload,
    _browser_ingest_health_from_items,
    _browser_extension_payload,
    _browser_tab_maintenance_payload,
    _extension_versions_match,
)


def test_extension_versions_match_ignores_v_prefix():
    assert _extension_versions_match("1.21.32", "v1.21.32")
    assert _extension_versions_match("v1.21.32", "1.21.32")
    assert _extension_versions_match("1.21.33", "1.21.32")
    assert not _extension_versions_match("1.21.28", "1.21.32")


def test_browser_ingest_health_from_items_marks_content_active(monkeypatch):
    monkeypatch.setenv("BROWSER_INGEST_ACTIVE_SECONDS", "600")

    health = _browser_ingest_health_from_items([
        {
            "platform": "instagram",
            "endpoint": "media",
            "observed_count": 12,
            "stored_count": 7,
            "last_seen_at": datetime(2026, 8, 8, tzinfo=timezone.utc),
            "age_seconds": 20,
        },
        {
            "platform": "tiktok",
            "endpoint": "browser_heartbeat",
            "observed_count": 1,
            "stored_count": 0,
            "last_seen_at": datetime(2026, 8, 8, tzinfo=timezone.utc),
            "age_seconds": 15,
        },
    ])

    assert health["state"] == "active"
    assert health["active"] is True
    assert health["heartbeat_active"] is True
    assert health["content_active"] is True
    assert health["active_platforms"] == ["instagram", "tiktok"]
    assert health["content_platforms"] == ["instagram"]


def test_browser_tab_maintenance_payload_reads_host_status(tmp_path):
    status = tmp_path / "browser_tab_maintenance_status.json"
    status.write_text(
        "\ufeff" + json.dumps({
            "checked_at": "2026-08-08T18:26:17.1804359+08:00",
            "state": "cdp_unavailable",
            "detail": "Unable to connect to the remote server",
            "cdp_url": "http://127.0.0.1:9222",
            "pid": 15168,
            "last_terminal_state": "cdp_unavailable",
            "consecutive_cdp_unavailable_count": 4,
            "cdp_unavailable_since": "2026-08-08T17:00:00+08:00",
            "diagnostics": {
                "reason": "chrome_running_without_cdp",
                "chrome_process_count": 26,
                "chrome_cdp_process_count": 0,
                "hint": "Start or restart the scraper Chrome with --remote-debugging-port=9222.",
            },
        }),
        encoding="utf-8",
    )

    payload = _browser_tab_maintenance_payload(status)

    assert payload["state"] == "cdp_unavailable"
    assert payload["detail"] == "Unable to connect to the remote server"
    assert payload["cdp_url"] == "http://127.0.0.1:9222"
    assert payload["pid"] == 15168
    assert payload["last_terminal_state"] == "cdp_unavailable"
    assert payload["consecutive_cdp_unavailable_count"] == 4
    assert payload["cdp_unavailable_since"] == "2026-08-08T17:00:00+08:00"
    assert payload["diagnostics"]["reason"] == "chrome_running_without_cdp"
    assert payload["diagnostics"]["chrome_process_count"] == 26
    assert payload["stale"] is False
    assert isinstance(payload["age_seconds"], int)


def test_browser_tab_maintenance_payload_ignores_missing_status(tmp_path):
    assert _browser_tab_maintenance_payload(tmp_path / "missing.json") is None


def test_browser_extension_fallback_keeps_cdp_issue_when_ingest_times_out(monkeypatch, tmp_path):
    status = tmp_path / "browser_tab_maintenance_status.json"
    status.write_text(
        json.dumps({
            "state": "cdp_unavailable",
            "detail": "Unable to connect to Chrome CDP",
            "cdp_url": "http://127.0.0.1:9222",
            "checked_at": "2026-08-08T18:26:17.1804359+08:00",
            "diagnostics": {
                "reason": "chrome_running_without_cdp",
                "hint": "Start scraper Chrome with CDP.",
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_api, "_BROWSER_TAB_MAINTENANCE_STATUS_PATH", str(status))

    payload = _browser_extension_fallback_payload("TimeoutError")

    assert payload["maintenance"]["state"] == "cdp_unavailable"
    assert payload["ingest_health"]["state"] == "unknown"
    assert payload["issues"][0]["kind"] == "browser_maintenance_cdp_unavailable"
    assert payload["issues"][0]["ingest_diagnostics_unavailable"] is True
    assert "TimeoutError" in payload["issues"][0]["detail"]


@pytest.mark.asyncio
async def test_browser_extension_payload_flags_stale_and_old_versions(monkeypatch):
    monkeypatch.setenv("UC_EXTENSION_EXPECTED_VERSION", "1.21.32")

    class FakeConn:
        async def fetchval(self, query: str, timeout: int | None = None):
            if "dm_hook_heartbeat" in query:
                return "dm_hook_heartbeat"
            if "browser_ingest_events" in query:
                return "browser_ingest_events"
            if "tiktok_browser_media_candidates" in query:
                return None
            if "tiktok_browser_revisit_queue" in query:
                return None
            if "browser_media_candidates" in query:
                return None
            if "browser_media_revisit_queue" in query:
                return None
            raise AssertionError(query)

        async def fetch(self, query: str, *args, timeout: int | None = None):
            if "FROM dm_hook_heartbeat" in query:
                return [
                    {
                        "platform": "instagram",
                        "last_seen_at": datetime(2026, 7, 27, tzinfo=timezone.utc),
                        "age_seconds": 3700,
                        "extension_version": "1.21.28",
                        "owner_count": 1,
                        "probes_sent": 10,
                        "samples_shipped": 2,
                    }
                ]
            if "FROM browser_ingest_events" in query:
                return [
                    {
                        "platform": "strava",
                        "endpoint": "strava_route_visit",
                        "requests": 2,
                        "observed_count": 2,
                        "stored_count": 0,
                        "last_seen_at": datetime(2026, 7, 27, tzinfo=timezone.utc),
                        "age_seconds": 45,
                        "extension_version": "1.21.28",
                    }
                ]
            raise AssertionError(query)

    payload = await _browser_extension_payload(FakeConn())

    assert payload["expected_version"] == "1.21.32"
    assert payload["hooks"][0]["version_ok"] is False
    assert payload["ingest"][0]["version_ok"] is False
    kinds = {issue["kind"] for issue in payload["issues"]}
    assert kinds == {"hook_stale", "extension_version_mismatch"}
    mismatch = [issue for issue in payload["issues"] if issue["kind"] == "extension_version_mismatch"]
    assert {issue["age_seconds"] for issue in mismatch} == {3700, 45}
    assert all(issue["last_seen_at"] for issue in mismatch)
    by_age = {issue["age_seconds"]: issue for issue in mismatch}
    assert by_age[3700]["needs_new_event"] is True
    assert "waiting for a fresh heartbeat" in by_age[3700]["detail"]
    assert by_age[45]["needs_new_event"] is False


@pytest.mark.asyncio
async def test_browser_extension_payload_includes_browser_maintenance_issue(monkeypatch, tmp_path):
    status = tmp_path / "browser_tab_maintenance_status.json"
    status.write_text(
        json.dumps({
            "state": "cdp_unavailable",
            "detail": "Unable to connect to the remote server",
            "cdp_url": "http://127.0.0.1:9222",
            "checked_at": "2026-08-08T18:26:17.1804359+08:00",
            "diagnostics": {
                "reason": "chrome_running_without_cdp",
                "chrome_process_count": 26,
                "chrome_cdp_process_count": 0,
                "hint": "Start or restart the scraper Chrome with --remote-debugging-port=9222.",
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_api, "_BROWSER_TAB_MAINTENANCE_STATUS_PATH", str(status))

    class FakeConn:
        async def fetchval(self, query: str, timeout: int | None = None):
            if "dm_hook_heartbeat" in query:
                return None
            if "browser_ingest_events" in query:
                return None
            if "tiktok_browser_media_candidates" in query:
                return None
            if "tiktok_browser_revisit_queue" in query:
                return None
            if "browser_media_candidates" in query:
                return None
            if "browser_media_revisit_queue" in query:
                return None
            raise AssertionError(query)

        async def fetch(self, query: str, *args, timeout: int | None = None):
            raise AssertionError(query)

    payload = await _browser_extension_payload(FakeConn())

    assert payload["maintenance"]["state"] == "cdp_unavailable"
    assert payload["maintenance"]["diagnostics"]["reason"] == "chrome_running_without_cdp"
    assert payload["issues"][0]["kind"] == "browser_maintenance_cdp_unavailable"
    assert payload["issues"][0]["cdp_url"] == "http://127.0.0.1:9222"
    assert payload["issues"][0]["diagnostics"]["chrome_cdp_process_count"] == 0
    assert "chrome_running_without_cdp" in payload["issues"][0]["detail"]


@pytest.mark.asyncio
async def test_browser_extension_payload_distinguishes_ingest_active_from_cdp_down(monkeypatch, tmp_path):
    status = tmp_path / "browser_tab_maintenance_status.json"
    status.write_text(
        json.dumps({
            "state": "cdp_unavailable",
            "detail": "Unable to connect to the remote server",
            "cdp_url": "http://127.0.0.1:9222",
            "checked_at": "2026-08-08T18:26:17.1804359+08:00",
            "diagnostics": {
                "reason": "chrome_running_without_cdp",
                "chrome_process_count": 26,
                "chrome_cdp_process_count": 0,
                "hint": "Start scraper Chrome with CDP.",
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_api, "_BROWSER_TAB_MAINTENANCE_STATUS_PATH", str(status))
    monkeypatch.setenv("BROWSER_INGEST_ACTIVE_SECONDS", "600")

    class FakeConn:
        async def fetchval(self, query: str, timeout: int | None = None):
            if "dm_hook_heartbeat" in query:
                return None
            if "SELECT to_regclass('browser_ingest_events')" in query:
                return "browser_ingest_events"
            if "FROM browser_ingest_events" in query:
                return None
            if "tiktok_browser_media_candidates" in query:
                return None
            if "tiktok_browser_revisit_queue" in query:
                return None
            if "browser_media_candidates" in query:
                return None
            if "browser_media_revisit_queue" in query:
                return None
            raise AssertionError(query)

        async def fetch(self, query: str, *args, timeout: int | None = None):
            if "WITH heartbeat AS" in query:
                return []
            if "FROM browser_ingest_events" in query:
                return [
                    {
                        "platform": "instagram",
                        "endpoint": "media",
                        "requests": 10,
                        "observed_count": 20,
                        "stored_count": 8,
                        "last_seen_at": datetime(2026, 8, 8, tzinfo=timezone.utc),
                        "age_seconds": 30,
                        "extension_version": "1.23.49",
                    },
                    {
                        "platform": "tiktok",
                        "endpoint": "browser_heartbeat",
                        "requests": 5,
                        "observed_count": 5,
                        "stored_count": 0,
                        "last_seen_at": datetime(2026, 8, 8, tzinfo=timezone.utc),
                        "age_seconds": 25,
                        "extension_version": "1.23.49",
                    },
                ]
            raise AssertionError(query)

    payload = await _browser_extension_payload(FakeConn())

    assert payload["ingest_health"]["state"] == "active"
    assert payload["ingest_health"]["active"] is True
    assert payload["ingest_health"]["heartbeat_active"] is True
    assert payload["ingest_health"]["content_active"] is True
    assert payload["ingest_health"]["active_platforms"] == ["instagram", "tiktok"]
    assert payload["ingest_health"]["content_platforms"] == ["instagram"]
    issue = payload["issues"][0]
    assert issue["kind"] == "browser_maintenance_cdp_unavailable"
    assert issue["extension_ingest_active"] is True
    assert "Browser extension ingest is still producing useful content" in issue["detail"]
    assert issue["detail"].count("Browser extension ingest is still producing useful content") == 1


@pytest.mark.asyncio
async def test_browser_extension_fallback_payload_uses_fast_ingest(monkeypatch, tmp_path):
    status = tmp_path / "browser_tab_maintenance_status.json"
    status.write_text(
        json.dumps({
            "state": "cdp_unavailable",
            "detail": "Unable to connect to the remote server",
            "checked_at": "2026-08-08T18:26:17.1804359+08:00",
            "cdp_url": "http://127.0.0.1:9222",
            "diagnostics": {"reason": "chrome_running_without_cdp"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_api, "_BROWSER_TAB_MAINTENANCE_STATUS_PATH", str(status))
    monkeypatch.setenv("BROWSER_INGEST_ACTIVE_SECONDS", "600")

    class FakeConn:
        async def fetchval(self, query: str, timeout: float | None = None):
            if "to_regclass('browser_ingest_events')" in query:
                return "browser_ingest_events"
            raise AssertionError(query)

        async def fetch(self, query: str, timeout: float | None = None):
            if "FROM browser_ingest_events" in query:
                return [
                    {
                        "platform": "instagram",
                        "endpoint": "media",
                        "requests": 4,
                        "observed_count": 10,
                        "stored_count": 8,
                        "last_seen_at": datetime(2026, 8, 8, tzinfo=timezone.utc),
                        "age_seconds": 12,
                        "extension_version": "1.23.50",
                    },
                    {
                        "platform": "x",
                        "endpoint": "browser_heartbeat",
                        "requests": 3,
                        "observed_count": 3,
                        "stored_count": 0,
                        "last_seen_at": datetime(2026, 8, 8, tzinfo=timezone.utc),
                        "age_seconds": 18,
                        "extension_version": "1.23.50",
                    },
                ]
            raise AssertionError(query)

    class FakePool:
        def __init__(self):
            self.conn = FakeConn()
            self.released = False

        async def acquire(self):
            return self.conn

        async def release(self, conn):
            assert conn is self.conn
            self.released = True

    fake_pool = FakePool()

    async def fake_get_pool():
        return fake_pool

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)

    payload = await _browser_extension_fallback_payload_with_fast_ingest("TimeoutError")

    assert fake_pool.released is True
    assert payload["fast_ingest_fallback"] is True
    assert payload["ingest_health"]["state"] == "active"
    assert payload["ingest_health"]["content_active"] is True
    assert payload["ingest_health"]["active_platforms"] == ["instagram", "x"]
    assert payload["ingest_health"]["content_platforms"] == ["instagram"]
    issue = payload["issues"][0]
    assert issue["kind"] == "browser_maintenance_cdp_unavailable"
    assert issue["extension_ingest_active"] is True
    assert issue["ingest_diagnostics_unavailable"] is False
    assert issue["detail"].count("Browser extension ingest is still producing useful content") == 1


@pytest.mark.asyncio
async def test_browser_extension_payload_calls_out_heartbeat_only_cdp_down(monkeypatch, tmp_path):
    status = tmp_path / "browser_tab_maintenance_status.json"
    status.write_text(
        json.dumps({
            "state": "cdp_unavailable",
            "detail": "Unable to connect to the remote server",
            "checked_at": "2026-08-08T18:26:17.1804359+08:00",
            "cdp_url": "http://127.0.0.1:9222",
            "diagnostics": {"reason": "chrome_running_without_cdp"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_api, "_BROWSER_TAB_MAINTENANCE_STATUS_PATH", str(status))
    monkeypatch.setenv("BROWSER_INGEST_ACTIVE_SECONDS", "600")

    class FakeConn:
        async def fetchval(self, query: str, timeout: int | None = None):
            if "dm_hook_heartbeat" in query:
                return None
            if "SELECT to_regclass('browser_ingest_events')" in query:
                return "browser_ingest_events"
            if "FROM browser_ingest_events" in query:
                return None
            if "tiktok_browser_media_candidates" in query:
                return None
            if "tiktok_browser_revisit_queue" in query:
                return None
            if "browser_media_candidates" in query:
                return None
            if "browser_media_revisit_queue" in query:
                return None
            raise AssertionError(query)

        async def fetch(self, query: str, *args, timeout: int | None = None):
            if "WITH heartbeat AS" in query:
                return []
            if "FROM browser_ingest_events" in query:
                return [
                    {
                        "platform": "instagram",
                        "endpoint": "browser_heartbeat",
                        "requests": 4,
                        "observed_count": 4,
                        "stored_count": 0,
                        "last_seen_at": datetime(2026, 8, 8, tzinfo=timezone.utc),
                        "age_seconds": 30,
                        "extension_version": "1.23.50",
                    },
                ]
            raise AssertionError(query)

    payload = await _browser_extension_payload(FakeConn())

    assert payload["ingest_health"]["active"] is True
    assert payload["ingest_health"]["heartbeat_active"] is True
    assert payload["ingest_health"]["content_active"] is False
    assert "heartbeats are active, but useful browser content is stale" in payload["ingest_health"]["note"]
    issue = payload["issues"][0]
    assert issue["kind"] == "browser_maintenance_cdp_unavailable"
    assert "Browser extension heartbeats are still active, but useful browser content is stale" in issue["detail"]


@pytest.mark.asyncio
async def test_browser_extension_payload_flags_stale_browser_maintenance(monkeypatch, tmp_path):
    status = tmp_path / "browser_tab_maintenance_status.json"
    status.write_text(
        json.dumps({
            "state": "ok",
            "detail": "audit and reload completed",
            "checked_at": "2026-08-08T18:26:17.1804359+08:00",
        }),
        encoding="utf-8",
    )
    old = time.time() - 7200
    os.utime(status, (old, old))
    monkeypatch.setattr(dashboard_api, "_BROWSER_TAB_MAINTENANCE_STATUS_PATH", str(status))
    monkeypatch.setattr(dashboard_api, "_BROWSER_TAB_MAINTENANCE_STALE_SECONDS", 2700)

    class FakeConn:
        async def fetchval(self, query: str, timeout: int | None = None):
            if "dm_hook_heartbeat" in query:
                return None
            if "browser_ingest_events" in query:
                return None
            if "tiktok_browser_media_candidates" in query:
                return None
            if "tiktok_browser_revisit_queue" in query:
                return None
            if "browser_media_candidates" in query:
                return None
            if "browser_media_revisit_queue" in query:
                return None
            raise AssertionError(query)

        async def fetch(self, query: str, *args, timeout: int | None = None):
            raise AssertionError(query)

    payload = await _browser_extension_payload(FakeConn())

    assert payload["maintenance"]["stale"] is True
    assert payload["maintenance"]["stale_after_seconds"] == 2700
    assert payload["issues"][0]["kind"] == "browser_maintenance_stale"


@pytest.mark.asyncio
async def test_browser_extension_payload_suppresses_old_endpoint_after_newer_current_signal(monkeypatch):
    monkeypatch.setenv("UC_EXTENSION_EXPECTED_VERSION", "1.21.33")

    class FakeConn:
        async def fetchval(self, query: str, timeout: int | None = None):
            if "dm_hook_heartbeat" in query:
                return None
            if "browser_ingest_events" in query:
                return "browser_ingest_events"
            if "tiktok_browser_media_candidates" in query:
                return None
            if "tiktok_browser_revisit_queue" in query:
                return None
            if "browser_media_candidates" in query:
                return None
            if "browser_media_revisit_queue" in query:
                return None
            raise AssertionError(query)

        async def fetch(self, query: str, *args, timeout: int | None = None):
            if "FROM browser_ingest_events" in query:
                return [
                    {
                        "platform": "strava",
                        "endpoint": "strava_route_visit",
                        "requests": 118,
                        "observed_count": 118,
                        "stored_count": 0,
                        "last_seen_at": datetime(2026, 7, 27, 22, tzinfo=timezone.utc),
                        "age_seconds": 3904,
                        "extension_version": "1.21.28",
                    },
                    {
                        "platform": "strava",
                        "endpoint": "browser_heartbeat",
                        "requests": 16,
                        "observed_count": 16,
                        "stored_count": 0,
                        "last_seen_at": datetime(2026, 7, 27, 23, tzinfo=timezone.utc),
                        "age_seconds": 30,
                        "extension_version": "1.21.33",
                    },
                ]
            raise AssertionError(query)

    payload = await _browser_extension_payload(FakeConn())

    assert len(payload["ingest"]) == 2
    assert {item["endpoint"]: item["version_ok"] for item in payload["ingest"]} == {
        "strava_route_visit": False,
        "browser_heartbeat": True,
    }
    assert payload["issues"] == []


@pytest.mark.asyncio
async def test_browser_extension_payload_suppresses_recent_old_endpoint_after_newer_current_signal(monkeypatch):
    monkeypatch.setenv("UC_EXTENSION_EXPECTED_VERSION", "1.21.55")

    class FakeConn:
        async def fetchval(self, query: str, timeout: int | None = None):
            if "dm_hook_heartbeat" in query:
                return None
            if "browser_ingest_events" in query:
                return "browser_ingest_events"
            if "tiktok_browser_media_candidates" in query:
                return None
            if "tiktok_browser_revisit_queue" in query:
                return None
            if "browser_media_candidates" in query:
                return None
            if "browser_media_revisit_queue" in query:
                return None
            raise AssertionError(query)

        async def fetch(self, query: str, *args, timeout: int | None = None):
            if "FROM browser_ingest_events" in query:
                return [
                    {
                        "platform": "instagram",
                        "endpoint": "media",
                        "requests": 100,
                        "observed_count": 200,
                        "stored_count": 80,
                        "last_seen_at": datetime(2026, 7, 31, 22, 34, tzinfo=timezone.utc),
                        "age_seconds": 45,
                        "extension_version": "1.21.53",
                    },
                    {
                        "platform": "instagram",
                        "endpoint": "browser_heartbeat",
                        "requests": 2,
                        "observed_count": 2,
                        "stored_count": 0,
                        "last_seen_at": datetime(2026, 7, 31, 22, 35, tzinfo=timezone.utc),
                        "age_seconds": 5,
                        "extension_version": "1.21.55",
                    },
                ]
            raise AssertionError(query)

    payload = await _browser_extension_payload(FakeConn())

    assert {item["endpoint"]: item["version_ok"] for item in payload["ingest"]} == {
        "media": False,
        "browser_heartbeat": True,
    }
    assert payload["issues"] == []


@pytest.mark.asyncio
async def test_browser_extension_payload_content_gap_ignores_manual_backend_probe(monkeypatch):
    monkeypatch.setenv("UC_EXTENSION_EXPECTED_VERSION", "1.22.7")
    seen_content_gap_query = None

    class FakeConn:
        async def fetchval(self, query: str, timeout: int | None = None):
            if "dm_hook_heartbeat" in query:
                return None
            if "browser_ingest_events" in query:
                return "browser_ingest_events"
            if "tiktok_browser_media_candidates" in query:
                return None
            if "tiktok_browser_revisit_queue" in query:
                return None
            if "browser_media_candidates" in query:
                return None
            if "browser_media_revisit_queue" in query:
                return None
            raise AssertionError(query)

        async def fetch(self, query: str, *args, timeout: int | None = None):
            nonlocal seen_content_gap_query
            if "WITH selected(platform) AS" in query:
                seen_content_gap_query = query
                return []
            if "FROM browser_ingest_events" in query:
                return []
            raise AssertionError(query)

    await _browser_extension_payload(FakeConn())

    assert seen_content_gap_query is not None
    assert "manual_backend_probe" in seen_content_gap_query
    assert "forced_recovery_started" in seen_content_gap_query


@pytest.mark.asyncio
async def test_browser_extension_payload_includes_media_candidate_diagnostics(monkeypatch):
    monkeypatch.setenv("UC_EXTENSION_EXPECTED_VERSION", "1.21.46")

    class FakeConn:
        async def fetchval(self, query: str, timeout: int | None = None):
            if "dm_hook_heartbeat" in query:
                return None
            if "browser_ingest_events" in query:
                return None
            if "tiktok_browser_media_candidates" in query:
                return None
            if "tiktok_browser_revisit_queue" in query:
                return None
            if "browser_media_candidates" in query:
                return "browser_media_candidates"
            if "browser_media_revisit_queue" in query:
                return "browser_media_revisit_queue"
            raise AssertionError(query)

        async def fetch(self, query: str, *args, timeout: int | None = None):
            if "FROM browser_media_candidates" in query:
                return [
                    {
                        "platform": "facebook",
                        "outcome": "tiny_thumbnail",
                        "candidates": 12,
                        "needs_revisit": 0,
                        "last_seen_at": datetime(2026, 7, 31, tzinfo=timezone.utc),
                        "age_seconds": 60,
                    },
                    {
                        "platform": "x",
                        "outcome": "browser_fetch_failed",
                        "candidates": 2,
                        "needs_revisit": 2,
                        "last_seen_at": datetime(2026, 7, 31, tzinfo=timezone.utc),
                        "age_seconds": 120,
                    },
                ]
            if "FROM browser_media_revisit_queue" in query:
                return [
                    {
                        "platform": "x",
                        "due": 2,
                        "claimed": 1,
                        "stale_claimed": 0,
                        "pending": 3,
                        "failed": 1,
                        "unavailable": 0,
                        "completed": 4,
                        "last_seen_at": datetime(2026, 7, 31, tzinfo=timezone.utc),
                    }
                ]
            raise AssertionError(query)

    payload = await _browser_extension_payload(FakeConn())

    assert payload["media_candidates"] == [
        {
            "platform": "facebook",
            "outcome": "tiny_thumbnail",
            "candidates": 12,
            "needs_revisit": 0,
            "last_seen_at": datetime(2026, 7, 31, tzinfo=timezone.utc),
            "age_seconds": 60,
        },
        {
            "platform": "x",
            "outcome": "browser_fetch_failed",
            "candidates": 2,
            "needs_revisit": 2,
            "last_seen_at": datetime(2026, 7, 31, tzinfo=timezone.utc),
            "age_seconds": 120,
        },
    ]
    assert payload["media_revisit_queue"] == [
        {
            "platform": "x",
            "due": 2,
            "claimed": 1,
            "stale_claimed": 0,
            "pending": 3,
            "failed": 1,
            "unavailable": 0,
            "completed": 4,
            "last_seen_at": datetime(2026, 7, 31, tzinfo=timezone.utc),
        }
    ]
