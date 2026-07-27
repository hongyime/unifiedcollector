from __future__ import annotations

from datetime import datetime, timedelta, timezone

import os

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

from src.dashboard.api import _source_matrix_blocker, _source_matrix_row


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
    assert row["blocker"]["kind"] == "none"
