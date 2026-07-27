from __future__ import annotations

from datetime import datetime, timezone
import os

import pytest

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

from src.dashboard.api import _browser_extension_payload, _extension_versions_match


def test_extension_versions_match_ignores_v_prefix():
    assert _extension_versions_match("1.21.32", "v1.21.32")
    assert _extension_versions_match("v1.21.32", "1.21.32")
    assert not _extension_versions_match("1.21.28", "1.21.32")


@pytest.mark.asyncio
async def test_browser_extension_payload_flags_stale_and_old_versions(monkeypatch):
    monkeypatch.setenv("UC_EXTENSION_EXPECTED_VERSION", "1.21.32")

    class FakeConn:
        async def fetchval(self, query: str, timeout: int | None = None):
            if "dm_hook_heartbeat" in query:
                return "dm_hook_heartbeat"
            if "browser_ingest_events" in query:
                return "browser_ingest_events"
            raise AssertionError(query)

        async def fetch(self, query: str, timeout: int | None = None):
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
