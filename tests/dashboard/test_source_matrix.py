from __future__ import annotations

from datetime import datetime, timedelta, timezone

import os

import pytest

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

from src.dashboard.api import (
    _SOURCE_MEDIA_TOTALS_CACHE,
    _rate_limit_cursor_payload,
    _source_matrix_blocker,
    _source_matrix_row,
    _source_window_totals,
    _source_media_totals,
)


def _source(status: str = "live", **extra):
    row = {
        "source": "instagram",
        "status": status,
        "collection_mode": "chrome extension + headless",
        "freshness_basis": "media_items.collected_at where source=instagram",
        "age_seconds": 60,
        "stale_after_seconds": 172800,
        "detail": "newest row is inside the freshness window",
        "source_health_status": "running",
        "source_health_error": None,
        "source_health_last_success_at": datetime(2026, 7, 28, 1, 0, tzinfo=timezone.utc),
        "source_health_updated_at": datetime(2026, 7, 28, 1, 5, tzinfo=timezone.utc),
    }
    row.update(extra)
    return row


def test_source_matrix_blocker_prioritizes_whatsapp_pairing():
    blocker = _source_matrix_blocker(
        _source(
            source="whatsapp",
            status="unpaired",
            bridge_status="unpaired",
            bridge_detail="bridge 1 needs QR",
        ),
        rate_row=None,
        cursor_row=None,
        extension_issues=[],
    )

    assert blocker["kind"] == "whatsapp_pairing"
    assert blocker["severity"] == "warning"
    assert "QR" in blocker["next_action"]


def test_source_matrix_blocker_reports_active_cooldown_before_extension_issue():
    blocker = _source_matrix_blocker(
        _source(),
        rate_row=None,
        cursor_row={
            "active_now": True,
            "active_until": datetime.now(timezone.utc) + timedelta(hours=2),
            "streak": 6,
        },
        extension_issues=[{"kind": "extension_version_mismatch", "detail": "old"}],
    )

    assert blocker["kind"] == "cooldown"
    assert blocker["severity"] == "warning"
    assert "cooldown" in blocker["summary"].lower()


def test_source_matrix_blocker_reports_extension_version_context():
    blocker = _source_matrix_blocker(
        _source(),
        rate_row=None,
        cursor_row=None,
        extension_issues=[
            {
                "kind": "extension_version_mismatch",
                "detail": "Browser ingest event came from an older extension bundle.",
                "endpoint": "strava_streams",
                "extension_version": "1.21.28",
                "expected_version": "1.21.33",
                "age_seconds": 45,
            }
        ],
    )

    assert blocker["kind"] == "extension_version_mismatch"
    assert blocker["severity"] == "warning"
    assert "v1.21.28" in blocker["summary"]
    assert "v1.21.33" in blocker["summary"]
    assert "45s ago" in blocker["summary"]
    assert "duplicate" in blocker["next_action"]


def test_source_matrix_blocker_reports_extension_hook_context_without_endpoint():
    blocker = _source_matrix_blocker(
        _source(),
        rate_row=None,
        cursor_row=None,
        extension_issues=[
            {
                "kind": "extension_version_mismatch",
                "detail": "Chrome extension hook is still running an older bundle.",
                "extension_version": "1.21.28",
                "expected_version": "1.21.33",
                "age_seconds": 502,
            }
        ],
    )

    assert "from hook" in blocker["summary"]
    assert "on;" not in blocker["summary"]


def test_rate_limit_cursor_payload_marks_expired_cursor_inactive():
    now = datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc)
    payload = _rate_limit_cursor_payload(
        {
            "service": "instagram_rate_limit",
            "last_processed_id": f"{(now - timedelta(minutes=5)).timestamp()}:9",
            "last_processed_at": now - timedelta(hours=1),
            "status": "blocked",
        },
        now,
    )

    assert payload["streak"] == 9
    assert payload["active_now"] is False
    assert payload["active_until"] < now


def test_source_matrix_blocker_reports_auth_error():
    blocker = _source_matrix_blocker(
        _source(),
        rate_row={
            "active_now": False,
            "access_errors": 1,
            "latest_status_code": 401,
            "latest_reason": "Playwright profile auth response",
        },
        cursor_row=None,
        extension_issues=[],
    )

    assert blocker["kind"] == "auth_or_access"
    assert blocker["severity"] == "error"
    assert "auth" in blocker["next_action"].lower()


def test_source_matrix_row_counts_and_live_blocker():
    row = _source_matrix_row(
        _source(),
        current_content={"records": 4, "messages": 0, "media_items": 7},
        current_rate={"rate_limits": 1, "access_errors": 0},
        day_content={"records": 40, "messages": 0, "media_items": 70},
        day_rate={"rate_limits": 2, "access_errors": 0},
        media_total={"total_media_items": 123, "total_media_bytes": 456},
        cursor_row=None,
        extension_issues=[],
    )

    assert row["source"] == "instagram"
    assert row["collection_methods"] == ["chrome extension", "headless cookies"]
    assert row["current_hour"]["records"] == 4
    assert row["current_hour"]["media_items"] == 7
    assert row["current_hour"]["rate_limits"] == 1
    assert row["last_24h"]["records"] == 40
    assert row["total_media_items"] == 123
    assert row["source_health_last_success_at"] == datetime(2026, 7, 28, 1, 0, tzinfo=timezone.utc)
    assert row["source_health_updated_at"] == datetime(2026, 7, 28, 1, 5, tzinfo=timezone.utc)
    assert row["blocker"]["kind"] == "none"


def test_source_window_totals_sums_counts_and_active_sources():
    rows = [
        {
            "source": "telegram",
            "current_hour": {
                "records": 8,
                "messages": 8,
                "media_items": 1,
                "rate_limits": 0,
                "access_errors": 0,
                "latest_record_at": datetime(2026, 7, 28, 1, 5, tzinfo=timezone.utc),
                "latest_media_at": None,
                "latest_event_at": None,
            },
        },
        {
            "source": "instagram",
            "current_hour": {
                "records": 0,
                "messages": 0,
                "media_items": 2,
                "rate_limits": 1,
                "access_errors": 1,
                "latest_record_at": None,
                "latest_media_at": datetime(2026, 7, 28, 1, 10, tzinfo=timezone.utc),
                "latest_event_at": datetime(2026, 7, 28, 1, 11, tzinfo=timezone.utc),
            },
        },
        {"source": "x", "current_hour": {}},
    ]

    out = _source_window_totals(rows, "current_hour")

    assert out["records"] == 8
    assert out["messages"] == 8
    assert out["media_items"] == 3
    assert out["rate_limits"] == 1
    assert out["access_errors"] == 1
    assert out["active_sources"] == 2
    assert out["total_activity"] == 13


class _MediaTotalsConn:
    async def fetchval(self, *_args, **_kwargs):
        return ["media_source_rollups", "media_items"]

    async def fetch(self, query, *_args, **_kwargs):
        assert "media_source_rollups" in query
        assert "FROM media_items" not in query
        return [
            {
                "source": "instagram",
                "total_media_items": 12,
                "total_media_bytes": 34,
                "latest_media_at": datetime(2026, 7, 28, tzinfo=timezone.utc),
            }
        ]


@pytest.mark.asyncio
async def test_source_media_totals_prefers_rollup_table():
    _SOURCE_MEDIA_TOTALS_CACHE.clear()

    out = await _source_media_totals(_MediaTotalsConn())

    assert out["instagram"]["total_media_items"] == 12
