from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import os

import pytest

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

from src.dashboard import api as dashboard_api
from src.dashboard.api import (
    _SOURCE_MATRIX_BROWSER_EXTENSION_TIMEOUT_SECONDS,
    _SOURCE_MATRIX_DAY_CONTENT_TIMEOUT_SECONDS,
    _SOURCE_MATRIX_MEDIA_TOTALS_TIMEOUT_SECONDS,
    _SOURCE_MEDIA_TOTALS_CACHE,
    _SOURCE_MATRIX_SECTION_CACHE,
    _SOURCE_MATRIX_YOUTUBE_BACKLOG_TIMEOUT_SECONDS,
    _activity_last_seen_at,
    _beeper_source_key,
    _extension_reload_target_from_url,
    _messaging_policy,
    _normalize_beeper_network,
    _rate_limit_cursor_payload,
    _release_dashboard_conn,
    _source_matrix_blocker,
    _source_matrix_section,
    _source_content_summary,
    _source_media_freshness,
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


def test_source_matrix_blocker_explains_paired_whatsapp_without_messages():
    blocker = _source_matrix_blocker(
        _source(
            source="whatsapp",
            status="stale",
            bridge_status="paired",
            bridge_detail="2 WhatsApp bridge slot(s) paired and ready.",
            freshness_basis="whatsapp_messages.collected_at",
        ),
        rate_row=None,
        cursor_row=None,
        extension_issues=[],
    )

    assert blocker["kind"] == "whatsapp_message_stale"
    assert blocker["severity"] == "warning"
    assert "bridge is alive" in blocker["summary"]
    assert "HistorySync" in blocker["next_action"]


def test_source_matrix_blocker_browser_watchdog_gives_tab_action():
    blocker = _source_matrix_blocker(
        _source(
            source="threads",
            status="degraded",
            source_health_status="degraded",
            source_health_error="browser capture stalled: browser content progress is 8601s old (> 3600s) (watchdog)",
        ),
        rate_row=None,
        cursor_row=None,
        extension_issues=[],
    )

    assert blocker["kind"] == "browser_capture_stalled"
    assert "refresh the threads browser tab" in blocker["next_action"]
    assert "Docker collector logs are secondary" in blocker["next_action"]

    x_blocker = _source_matrix_blocker(
        _source(
            source="x",
            status="degraded",
            source_health_status="degraded",
            source_health_error="browser content progress is 14124s old (> 3600s)",
        ),
        rate_row=None,
        cursor_row=None,
        extension_issues=[],
    )

    assert x_blocker["kind"] == "browser_capture_stalled"
    assert "refresh the x browser tab" in x_blocker["next_action"]

    detail_only_blocker = _source_matrix_blocker(
        _source(
            source="x",
            status="degraded",
            source_health_status=None,
            source_health_error=None,
            detail="browser content progress is 14612s old (> 3600s)",
        ),
        rate_row=None,
        cursor_row=None,
        extension_issues=[],
    )

    assert detail_only_blocker["kind"] == "browser_capture_stalled"
    assert detail_only_blocker["summary"] == "browser content progress is 14612s old (> 3600s)"
    assert "refresh the x browser tab" in detail_only_blocker["next_action"]

    auth_wall_blocker = _source_matrix_blocker(
        _source(
            source="x",
            status="degraded",
            source_health_status="degraded",
            source_health_error="browser capture stalled: browser content progress is 14612s old (> 3600s) (watchdog)",
            browser_url="https://x.com/i/flow/login?redirect_after_login=%2Fhome",
            browser_content_stale=True,
        ),
        rate_row=None,
        cursor_row=None,
        extension_issues=[],
    )

    assert auth_wall_blocker["kind"] == "auth_wall"
    assert "login flow" in auth_wall_blocker["summary"]
    assert "Do not chase Docker logs" in auth_wall_blocker["next_action"]

    cleared_blocker = _source_matrix_blocker(
        _source(
            source="x",
            status="live",
            source_health_status="degraded",
            source_health_error="browser capture stalled: browser content progress is 9000s old (> 3600s) (watchdog)",
            browser_content_stale=False,
        ),
        rate_row=None,
        cursor_row=None,
        extension_issues=[],
    )

    assert cleared_blocker["kind"] == "none"


def test_source_matrix_expensive_sections_have_bounded_default_timeouts():
    assert _SOURCE_MATRIX_DAY_CONTENT_TIMEOUT_SECONDS <= 8
    assert _SOURCE_MATRIX_MEDIA_TOTALS_TIMEOUT_SECONDS <= 8
    assert _SOURCE_MATRIX_YOUTUBE_BACKLOG_TIMEOUT_SECONDS <= 8
    assert _SOURCE_MATRIX_BROWSER_EXTENSION_TIMEOUT_SECONDS <= 6


@pytest.mark.asyncio
async def test_release_dashboard_conn_swallows_cancelled_release():
    class Pool:
        async def release(self, conn):
            raise asyncio.CancelledError()

    await _release_dashboard_conn(Pool(), object(), "test source matrix")


@pytest.mark.asyncio
async def test_source_rate_summary_counts_cooldown_200_as_rate_limit(monkeypatch):
    monkeypatch.setattr(
        dashboard_api,
        "_existing_public_tables",
        AsyncMock(return_value={"rate_limit_events"}),
    )

    active_until = datetime.now(timezone.utc) + timedelta(minutes=20)

    class Conn:
        async def fetch(self, query, *args, **kwargs):
            assert "cooldown_seconds IS NOT NULL" in query
            return [
                {
                    "source": "tiktok",
                    "rate_limits": 1,
                    "access_errors": 0,
                    "latest_account": "tiktok_hongyime",
                    "latest_scope": "profile_metadata",
                    "latest_status_code": 200,
                    "latest_reason": "TikTok profile metadata challenge wall: challenge",
                    "latest_event_at": datetime.now(timezone.utc),
                    "active_until": active_until,
                }
            ]

    out = await dashboard_api._source_rate_summary(Conn(), "date_trunc('hour', now())")

    assert out["tiktok"]["rate_limits"] == 1
    assert out["tiktok"]["access_errors"] == 0
    assert out["tiktok"]["active_now"] is True


def test_messaging_coverage_normalizes_unknown_beeper_messages_from_chat_network():
    assert _normalize_beeper_network("unknown", "Discord") == "Discord"
    assert _normalize_beeper_network("", "WhatsApp") == "WhatsApp"
    assert _normalize_beeper_network(None, None) == "Unmapped Beeper"
    assert _beeper_source_key("Discord") == ("beeper_discord", "Beeper / Discord")
    assert _beeper_source_key("Slack") == ("beeper_slack", "Beeper / Slack")
    assert _messaging_policy("telegram").startswith("telegram native is canonical")
    assert "native collector" in _messaging_policy(None)


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


def test_source_matrix_blocker_reports_extension_waiting_for_new_event():
    blocker = _source_matrix_blocker(
        _source(),
        rate_row=None,
        cursor_row=None,
        extension_issues=[
            {
                "kind": "extension_version_mismatch",
                "detail": "Last browser ingest event used an older extension bundle; waiting for a fresh event.",
                "endpoint": "strava_route_visit",
                "extension_version": "1.21.28",
                "expected_version": "1.21.33",
                "age_seconds": 1288,
                "needs_new_event": True,
            }
        ],
    )

    assert "No newer signal has arrived" in blocker["summary"]
    assert "21m ago" in blocker["summary"]
    assert "one fresh signal" in blocker["next_action"]


def test_source_matrix_blocker_reports_browser_content_stale_without_reload_first():
    blocker = _source_matrix_blocker(
        _source(source="x"),
        rate_row=None,
        cursor_row=None,
        extension_issues=[
            {
                "platform": "x",
                "kind": "browser_content_stale",
                "detail": "Browser tab heartbeat is fresh, but no useful content has arrived.",
                "heartbeat_age_seconds": 80,
                "content_age_seconds": 7200,
                "stale_after_seconds": 3600,
                "url": "https://x.com/home",
            }
        ],
    )

    assert blocker["kind"] == "browser_content_stale"
    assert "heartbeat is fresh" in blocker["summary"]
    assert "content is stale" in blocker["summary"]
    assert "do not reload it first" in blocker["next_action"]
    assert "forced scrape pass" in blocker["next_action"]


def test_source_matrix_blocker_prioritizes_stale_content_over_old_extension_signal():
    blocker = _source_matrix_blocker(
        _source(source="x"),
        rate_row=None,
        cursor_row=None,
        extension_issues=[
            {
                "platform": "x",
                "kind": "extension_version_mismatch",
                "detail": "Browser ingest event came from an older extension bundle.",
                "endpoint": "browser_heartbeat",
                "extension_version": "1.22.2",
                "expected_version": "1.22.3",
                "age_seconds": 120,
            },
            {
                "platform": "x",
                "kind": "browser_content_stale",
                "detail": "Browser tab heartbeat is fresh, but no useful content has arrived.",
                "heartbeat_age_seconds": 120,
                "content_age_seconds": 7200,
                "stale_after_seconds": 3600,
                "url": "https://x.com/home",
            },
        ],
    )

    assert blocker["kind"] == "browser_content_stale"
    assert "useful content is stale" in blocker["summary"]
    assert "do not reload it first" in blocker["next_action"]


def test_source_matrix_blocker_uses_extension_reload_url_when_available():
    extension_id, reload_url = _extension_reload_target_from_url(
        "chrome-extension://pkmdmcklnjdeocoeigmlakhomhhcpafb/background.js"
    )

    blocker = _source_matrix_blocker(
        _source(),
        rate_row=None,
        cursor_row=None,
        extension_issues=[
            {
                "kind": "extension_version_mismatch",
                "endpoint": "browser_heartbeat",
                "extension_version": "1.21.45",
                "expected_version": "1.21.46",
                "needs_new_event": True,
                "reload_url": reload_url,
            }
        ],
    )

    assert extension_id == "pkmdmcklnjdeocoeigmlakhomhhcpafb"
    assert reload_url == "chrome-extension://pkmdmcklnjdeocoeigmlakhomhhcpafb/tabs.html?reload=1"
    assert reload_url in blocker["next_action"]
    assert "unpacked extension path" in blocker["next_action"]


def test_source_matrix_blocker_explains_instagram_dm_hook_stale():
    _extension_id, reload_url = _extension_reload_target_from_url(
        "chrome-extension://pkmdmcklnjdeocoeigmlakhomhhcpafb/background.js"
    )

    blocker = _source_matrix_blocker(
        _source(source="instagram"),
        rate_row=None,
        cursor_row=None,
        extension_issues=[
            {
                "platform": "instagram",
                "kind": "hook_stale",
                "detail": "Chrome extension DM hook heartbeat is older than 1 hour.",
                "age_seconds": 7200,
                "reload_url": reload_url,
            }
        ],
    )

    assert blocker["kind"] == "hook_stale"
    assert "Direct inbox" in blocker["next_action"]
    assert "normal browser heartbeats" in blocker["next_action"]
    assert reload_url in blocker["next_action"]


def test_source_matrix_blocker_treats_quiet_beeper_subsource_as_coverage_gap():
    blocker = _source_matrix_blocker(
        _source(
            source="beeper_linkedin",
            parent_source="beeper",
            status="stale",
            collection_mode="messaging bridge",
            detail="Beeper / LinkedIn via Beeper: 0 messages in the last 7 days, 12 chats, 8 people.",
        ),
        rate_row=None,
        cursor_row=None,
        extension_issues=[],
    )

    assert blocker["kind"] == "quiet_beeper_subsource"
    assert blocker["severity"] == "ok"
    assert "LinkedIn" in blocker["summary"]
    assert "expected new messages" in blocker["next_action"]


def test_source_matrix_blocker_still_warns_for_stale_parent_beeper_collector():
    blocker = _source_matrix_blocker(
        _source(
            source="beeper",
            status="stale",
            detail="Beeper has no recent source rows.",
        ),
        rate_row=None,
        cursor_row=None,
        extension_issues=[],
    )

    assert blocker["kind"] == "stale"
    assert blocker["severity"] == "warning"


def test_source_matrix_row_labels_quiet_beeper_subsource_without_media_warning():
    row = _source_matrix_row(
        _source(
            source="beeper_linkedin",
            parent_source="beeper",
            status="stale",
            collection_mode="messaging bridge",
            detail="Beeper / LinkedIn via Beeper: 2 chats, 3 people.",
        ),
        current_content=None,
        current_rate=None,
        day_content=None,
        day_rate=None,
        media_total={"total_media_items": 0, "total_media_bytes": 0, "latest_media_at": None},
        cursor_row=None,
        extension_issues=[],
        now=datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc),
    )

    assert row["status"] == "stale"
    assert row["status_label"] == "quiet"
    assert row["status_severity"] == "ok"
    assert row["blocker"]["kind"] == "quiet_beeper_subsource"
    assert row["blocker"]["severity"] == "ok"
    assert row["media_freshness"]["status"] == "quiet"
    assert row["media_freshness"]["severity"] == "ok"
    assert "media silence is expected" in row["media_freshness"]["summary"]


def test_source_matrix_row_does_not_warn_for_live_beeper_subsource_without_media():
    row = _source_matrix_row(
        _source(
            source="beeper_slack",
            parent_source="beeper",
            status="live",
            collection_mode="messaging bridge",
            detail="Beeper / Slack via Beeper: recent messages.",
        ),
        current_content={"records": 7, "messages": 7, "media_items": 0},
        current_rate=None,
        day_content={"records": 20, "messages": 20, "media_items": 0},
        day_rate=None,
        media_total={
            "total_media_items": 12,
            "total_media_bytes": 1200,
            "latest_media_at": datetime(2026, 7, 26, 1, 30, tzinfo=timezone.utc),
        },
        cursor_row=None,
        extension_issues=[],
        now=datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc),
    )

    assert row["status"] == "live"
    assert row["status_label"] == "live"
    assert row["media_freshness"]["status"] == "quiet"
    assert row["media_freshness"]["severity"] == "ok"
    assert "Messages are flowing" in row["media_freshness"]["summary"]


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
        _source(status="stale"),
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


def test_source_matrix_blocker_reports_recent_429_as_rate_limit_not_auth():
    blocker = _source_matrix_blocker(
        _source(status="stale"),
        rate_row={
            "active_now": False,
            "access_errors": 1,
            "latest_status_code": 429,
            "latest_account": "tiktok_hongyime",
            "latest_scope": "gallery-dl_local",
            "latest_reason": "local tool output matched rate-limit signature",
        },
        cursor_row=None,
        extension_issues=[],
    )

    assert blocker["kind"] == "rate_limit_recent"
    assert blocker["severity"] == "warning"
    assert "429" in blocker["summary"]
    assert "bad login" in blocker["next_action"]


def test_source_matrix_blocker_prioritizes_extension_action_over_inactive_429():
    blocker = _source_matrix_blocker(
        _source(status="degraded"),
        rate_row={
            "active_now": False,
            "access_errors": 1,
            "latest_status_code": "429",
            "latest_reason": "local tool output matched rate-limit signature",
        },
        cursor_row=None,
        extension_issues=[
            {
                "kind": "extension_version_mismatch",
                "endpoint": "browser_heartbeat",
                "extension_version": "1.21.95",
                "expected_version": "1.21.97",
                "needs_new_event": True,
            }
        ],
    )

    assert blocker["kind"] == "extension_version_mismatch"
    assert blocker["severity"] == "warning"
    assert "expected v1.21.97" in blocker["summary"]


def test_source_matrix_blocker_does_not_block_live_source_for_rotated_auth_event():
    blocker = _source_matrix_blocker(
        _source(status="live"),
        rate_row={
            "active_now": False,
            "access_errors": 1,
            "latest_status_code": 401,
            "latest_reason": "Playwright profile auth response",
        },
        cursor_row=None,
        extension_issues=[],
    )

    assert blocker["kind"] == "none"
    assert blocker["severity"] == "ok"


def test_source_matrix_row_counts_and_live_blocker():
    now = datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc)
    row = _source_matrix_row(
        _source(),
        current_content={"records": 4, "messages": 0, "media_items": 7},
        current_rate={"rate_limits": 1, "access_errors": 0},
        day_content={"records": 40, "messages": 0, "media_items": 70},
        day_rate={"rate_limits": 2, "access_errors": 0},
        media_total={
            "total_media_items": 123,
            "total_media_bytes": 456,
            "latest_media_at": now - timedelta(minutes=5),
        },
        cursor_row=None,
        extension_issues=[],
        now=now,
    )

    assert row["source"] == "instagram"
    assert row["collection_methods"] == ["chrome extension", "headless cookies"]
    assert row["current_hour"]["records"] == 4
    assert row["current_hour"]["media_items"] == 7
    assert row["current_hour"]["rate_limits"] == 1
    assert row["last_24h"]["records"] == 40
    assert row["total_media_items"] == 123
    assert row["media_freshness"]["status"] == "fresh"
    assert row["media_freshness"]["current_hour_items"] == 7
    assert row["source_health_last_success_at"] == datetime(2026, 7, 28, 1, 0, tzinfo=timezone.utc)
    assert row["source_health_updated_at"] == datetime(2026, 7, 28, 1, 5, tzinfo=timezone.utc)
    # activity_last_seen_at is now - age_seconds. age_seconds is 60 in _source().
    assert row["activity_last_seen_at"] == now - timedelta(seconds=60)
    assert row["blocker"]["kind"] == "none"


def test_source_matrix_row_labels_live_source_degraded_when_extension_blocked():
    row = _source_matrix_row(
        _source(),
        current_content={"records": 0, "messages": 0, "media_items": 0},
        current_rate=None,
        day_content={"records": 9, "messages": 0, "media_items": 316},
        day_rate=None,
        media_total={"total_media_items": 137484},
        cursor_row=None,
        extension_issues=[
            {
                "kind": "extension_version_mismatch",
                "detail": "Last browser ingest event used an older extension bundle.",
                "endpoint": "browser_heartbeat",
                "extension_version": "1.21.62",
                "expected_version": "1.21.63",
                "needs_new_event": True,
            }
        ],
    )

    assert row["status"] == "live"
    assert row["status_label"] == "degraded"
    assert row["status_severity"] == "warning"
    assert row["blocker"]["kind"] == "extension_version_mismatch"


def test_source_matrix_row_suppresses_stale_browser_content_when_current_media_flows():
    row = _source_matrix_row(
        _source(source="threads"),
        current_content={"records": 0, "messages": 0, "media_items": 10},
        current_rate=None,
        day_content={"records": 20, "messages": 0, "media_items": 18},
        day_rate=None,
        media_total={"total_media_items": 3417},
        cursor_row=None,
        extension_issues=[
            {
                "kind": "browser_content_stale",
                "platform": "threads",
                "detail": "Browser tab heartbeat is fresh, but no useful content has arrived.",
                "content_age_seconds": 7200,
            }
        ],
    )

    assert row["status"] == "live"
    assert row["status_label"] == "live"
    assert row["blocker"]["kind"] == "none"
    assert row["extension_issues"] == []


def test_source_matrix_row_does_not_block_fresh_media_for_old_extension_event():
    row = _source_matrix_row(
        _source(source="threads"),
        current_content={"records": 0, "messages": 0, "media_items": 88},
        current_rate=None,
        day_content={"records": 20, "messages": 0, "media_items": 120},
        day_rate=None,
        media_total={"total_media_items": 3417},
        cursor_row=None,
        extension_issues=[
            {
                "kind": "extension_version_mismatch",
                "platform": "threads",
                "endpoint": "media",
                "detail": "Browser ingest event came from an older extension bundle.",
                "extension_version": "1.22.5",
                "expected_version": "1.22.6",
                "age_seconds": 1,
            }
        ],
    )

    assert row["status"] == "live"
    assert row["status_label"] == "live"
    assert row["blocker"]["kind"] == "none"
    assert row["extension_issues"][0]["kind"] == "extension_version_mismatch"


def test_source_matrix_reports_media_quiet_without_blocking_live_rows():
    now = datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc)
    row = _source_matrix_row(
        _source(source="facebook", freshness_basis="facebook_posts.collected_at"),
        current_content={"records": 12, "messages": 0, "media_items": 0},
        current_rate=None,
        day_content={"records": 30, "messages": 0, "media_items": 0},
        day_rate=None,
        media_total={
            "total_media_items": 29,
            "total_media_bytes": 1787223,
            "latest_media_at": now - timedelta(days=2),
        },
        cursor_row=None,
        extension_issues=[],
        now=now,
    )

    assert row["status"] == "live"
    assert row["blocker"]["kind"] == "none"
    assert row["media_freshness"]["status"] == "quiet"
    assert row["media_freshness"]["severity"] == "warning"
    assert "No media files in the last 24h" in row["media_freshness"]["summary"]


def test_source_matrix_reports_media_totals_unknown_without_false_zero_gap():
    now = datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc)
    row = _source_matrix_row(
        _source(source="facebook", freshness_basis="facebook_posts.collected_at"),
        current_content={"records": 12, "messages": 0, "media_items": 0},
        current_rate=None,
        day_content={"records": 30, "messages": 0, "media_items": 0},
        day_rate=None,
        media_total={
            "stats_unavailable": True,
            "stats_error": "TimeoutError",
        },
        cursor_row=None,
        extension_issues=[],
        now=now,
    )

    assert row["media_stats_unavailable"] is True
    assert row["media_stats_error"] == "TimeoutError"
    assert row["total_media_items"] == 0
    assert row["media_freshness"]["status"] == "unknown"
    assert row["media_freshness"]["severity"] == "warning"
    assert "not claiming this source has zero media" in row["media_freshness"]["summary"]
    assert "Refresh after DB load drops" in row["media_freshness"]["next_action"]


def test_source_matrix_uses_latest_media_when_window_counts_timeout():
    now = datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc)
    row = _source_matrix_row(
        _source(source="tiktok", freshness_basis="media_items.collected_at where source=tiktok"),
        current_content=None,
        current_rate=None,
        day_content=None,
        day_rate=None,
        media_total={
            "total_media_items": 24027,
            "total_media_bytes": 50_000_000_000,
            "latest_media_at": now - timedelta(minutes=26),
        },
        cursor_row=None,
        extension_issues=[],
        now=now,
    )

    assert row["media_freshness"]["status"] == "recent"
    assert row["media_freshness"]["severity"] == "ok"
    assert "Latest media was 26m ago" in row["media_freshness"]["summary"]
    assert "No media files in the last 24h" not in row["media_freshness"]["summary"]


def test_source_matrix_reports_youtube_video_backlog_as_blocker():
    now = datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc)
    row = _source_matrix_row(
        _source(source="youtube", freshness_basis="youtube_videos.collected_at"),
        current_content={"records": 12, "messages": 0, "media_items": 0},
        current_rate=None,
        day_content={"records": 500, "messages": 0, "media_items": 0},
        day_rate=None,
        media_total={
            "total_media_items": 34416,
            "total_media_bytes": 146170230207,
            "latest_media_at": now - timedelta(hours=28),
        },
        cursor_row=None,
        extension_issues=[],
        now=now,
        media_backlog={
            "total_videos": 22476,
            "missing_thumbnails": 1,
            "missing_videos": 10591,
            "missing_videos_touched_24h": 85,
            "eligible_missing_videos": 8327,
            "eligible_missing_videos_never_attempted": 7900,
            "eligible_missing_videos_touched_24h": 44,
            "over_duration_missing_videos": 2188,
            "placeholder_missing_videos": 8,
            "duration_cap_seconds": 1080,
        },
    )

    assert row["blocker"]["kind"] == "media_backlog"
    assert row["blocker"]["severity"] == "warning"
    assert "8,327 eligible" in row["blocker"]["summary"]
    assert "out of 10,591" in row["blocker"]["summary"]
    assert "2,188 >18m" in row["blocker"]["summary"]
    assert "8 live/scheduled" in row["blocker"]["summary"]
    assert "7,900 have never had a video download attempt" in row["blocker"]["summary"]
    assert row["media_backlog"]["eligible_missing_videos_touched_24h"] == 44


def test_source_media_freshness_does_not_warn_for_github_commits():
    now = datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc)
    freshness = _source_media_freshness(
        "github",
        {"records": 20, "media_items": 0},
        {"records": 80, "media_items": 0},
        {"total_media_items": 10, "latest_media_at": now - timedelta(days=3)},
        now=now,
    )

    assert freshness["status"] == "not_primary"
    assert freshness["severity"] == "ok"


def test_activity_last_seen_at_helper_handles_edge_values():
    now = datetime(2026, 8, 4, 8, 30, tzinfo=timezone.utc)

    assert _activity_last_seen_at(None, now) is None
    assert _activity_last_seen_at("not-a-number", now) is None
    assert _activity_last_seen_at(0, now) == now
    assert _activity_last_seen_at(2712, now) == now - timedelta(seconds=2712)


def test_source_matrix_row_separates_source_activity_from_media_for_text_heavy_source():
    """Regression: whatsapp/beeper are text-heavy realtime sources whose media
    rows are naturally rare. The freshness panel must surface
    ``activity_last_seen_at`` (source liveness) and ``latest_media_at`` (media
    row cadence) as distinct fields, so a 17h-old newest media file does not
    make the source itself look 900+ min stale when messages are flowing every
    minute. compute_liveness already reads whatsapp_messages, not media_items —
    this locks the response schema to that intent.
    """
    now = datetime(2026, 8, 4, 8, 30, tzinfo=timezone.utc)
    row = _source_matrix_row(
        _source(
            source="whatsapp",
            status="live",
            collection_mode="whatsapp bridge",
            freshness_basis="whatsapp_messages.collected_at",
            age_seconds=2712,  # ~45 min — messages flowing
            stale_after_seconds=14400,
        ),
        current_content={"records": 42, "messages": 42, "media_items": 0},
        current_rate=None,
        day_content={"records": 800, "messages": 800, "media_items": 0},
        day_rate=None,
        media_total={
            "total_media_items": 5320,
            "total_media_bytes": 2_000_000_000,
            "latest_media_at": now - timedelta(hours=17),  # media naturally rare
        },
        cursor_row=None,
        extension_issues=[],
        now=now,
    )

    # Source activity is fresh (compute_liveness read whatsapp_messages).
    assert row["status"] == "live"
    assert row["age_seconds"] == 2712
    assert row["activity_last_seen_at"] == now - timedelta(seconds=2712)
    assert row["freshness_basis"] == "whatsapp_messages.collected_at"

    # Media row is old — expected for a text-heavy source, kept as a separate
    # signal so it does NOT feed into source liveness.
    assert row["latest_media_at"] == now - timedelta(hours=17)
    assert row["media_freshness"]["status"] == "recent"
    assert row["media_freshness"]["severity"] == "ok"
    assert row["blocker"]["kind"] == "none"


def test_source_matrix_row_activity_last_seen_at_is_null_when_age_unknown():
    now = datetime(2026, 8, 4, 8, 30, tzinfo=timezone.utc)
    row = _source_matrix_row(
        _source(source="beeper", status="unknown", age_seconds=None),
        current_content=None,
        current_rate=None,
        day_content=None,
        day_rate=None,
        media_total={"total_media_items": 0, "total_media_bytes": 0, "latest_media_at": None},
        cursor_row=None,
        extension_issues=[],
        now=now,
    )

    assert row["age_seconds"] is None
    assert row["activity_last_seen_at"] is None


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


def test_source_window_totals_preserves_media_unavailable_flag():
    rows = [
        {
            "source": "telegram",
            "last_24h": {
                "records": 8,
                "messages": 8,
                "media_items": 0,
                "rate_limits": 0,
                "access_errors": 0,
                "latest_record_at": datetime(2026, 7, 28, 1, 5, tzinfo=timezone.utc),
                "latest_media_at": None,
                "latest_event_at": None,
                "media_stats_unavailable": True,
            },
        }
    ]

    out = _source_window_totals(rows, "last_24h")

    assert out["records"] == 8
    assert out["media_items"] == 0
    assert out["media_stats_unavailable"] is True


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


class _MediaTotalsSlowBeeperConn:
    async def fetchval(self, *_args, **_kwargs):
        return ["media_source_rollups", "media_items"]

    async def fetch(self, query, *_args, **_kwargs):
        if "media_source_rollups" in query:
            return [
                {
                    "source": "x",
                    "total_media_items": 23,
                    "total_media_bytes": 66_439_203,
                    "latest_media_at": datetime(2026, 7, 28, tzinfo=timezone.utc),
                }
            ]
        if "FROM media_items" in query:
            raise TimeoutError("slow optional beeper split")
        raise AssertionError(query)


@pytest.mark.asyncio
async def test_source_media_totals_prefers_rollup_table():
    _SOURCE_MEDIA_TOTALS_CACHE.clear()

    out = await _source_media_totals(_MediaTotalsConn())

    assert out["instagram"]["total_media_items"] == 12


@pytest.mark.asyncio
async def test_source_media_totals_keeps_rollups_when_beeper_split_times_out():
    _SOURCE_MEDIA_TOTALS_CACHE.clear()

    out = await _source_media_totals(_MediaTotalsSlowBeeperConn())

    assert out["x"]["total_media_items"] == 23
    assert "__stats_unavailable__" not in out
    assert out["__beeper_subsource_stats_unavailable__"] is True


@pytest.mark.asyncio
async def test_source_content_summary_groups_rows_and_keeps_media_separate(monkeypatch):
    async def no_beeper_subsources(conn, since_sql, before_sql=None):
        return {}

    monkeypatch.setattr(
        "src.dashboard.api._beeper_subsource_content_summary",
        no_beeper_subsources,
    )

    class FakeConn:
        def __init__(self):
            self.content_queries = []

        async def fetchval(self, query, *args, timeout=None):
            assert "information_schema.tables" in query
            return ["telegram_messages", "youtube_videos", "media_items"]

        async def fetch(self, query, *args, timeout=None):
            self.content_queries.append((query, timeout))
            if "WITH raw AS" in query:
                assert "UNION ALL" in query
                assert "telegram_messages" in query
                assert "youtube_videos" in query
                assert "FROM media_items" not in query
                return [
                    {
                        "source": "telegram",
                        "records": 12,
                        "messages": 12,
                        "media_items": 0,
                        "latest_record_at": datetime(2026, 7, 28, 1, 5, tzinfo=timezone.utc),
                        "latest_media_at": None,
                    },
                    {
                        "source": "youtube",
                        "records": 8,
                        "messages": 0,
                        "media_items": 0,
                        "latest_record_at": datetime(2026, 7, 28, 1, 3, tzinfo=timezone.utc),
                        "latest_media_at": None,
                    },
                ]
            assert "FROM media_items" in query
            assert "GROUP BY source" in query
            return [
                {
                    "source": "telegram",
                    "records": 0,
                    "messages": 0,
                    "media_items": 3,
                    "latest_record_at": None,
                    "latest_media_at": datetime(2026, 7, 28, 1, 4, tzinfo=timezone.utc),
                },
                {
                    "source": "youtube",
                    "records": 0,
                    "messages": 0,
                    "media_items": 2,
                    "latest_record_at": None,
                    "latest_media_at": datetime(2026, 7, 28, 1, 2, tzinfo=timezone.utc),
                },
            ]

    conn = FakeConn()
    out = await _source_content_summary(conn, "now() - interval '24 hours'")

    assert len(conn.content_queries) == 2
    assert conn.content_queries[0][1] >= 1.0
    assert conn.content_queries[1][1] >= 1.0
    assert out["telegram"]["records"] == 12
    assert out["telegram"]["messages"] == 12
    assert out["telegram"]["media_items"] == 3
    assert out["telegram"]["latest_record_at"] == datetime(2026, 7, 28, 1, 5, tzinfo=timezone.utc)
    assert out["telegram"]["latest_media_at"] == datetime(2026, 7, 28, 1, 4, tzinfo=timezone.utc)
    assert out["youtube"]["records"] == 8
    assert out["youtube"]["messages"] == 0
    assert out["youtube"]["media_items"] == 2


@pytest.mark.asyncio
async def test_source_content_summary_does_not_false_zero_media_on_timeout(monkeypatch):
    async def no_beeper_subsources(conn, since_sql, before_sql=None):
        return {}

    monkeypatch.setattr(
        "src.dashboard.api._beeper_subsource_content_summary",
        no_beeper_subsources,
    )

    class FakeConn:
        def __init__(self):
            self.content_queries = []

        async def fetchval(self, query, *args, timeout=None):
            assert "information_schema.tables" in query
            return ["telegram_messages", "media_items"]

        async def fetch(self, query, *args, timeout=None):
            self.content_queries.append((query, timeout))
            if "FROM media_items" in query:
                raise asyncio.TimeoutError()
            return [
                {
                    "source": "telegram",
                    "records": 12,
                    "messages": 12,
                    "media_items": 0,
                    "latest_record_at": datetime(2026, 7, 28, 1, 5, tzinfo=timezone.utc),
                    "latest_media_at": None,
                },
            ]

    conn = FakeConn()
    out = await _source_content_summary(conn, "now() - interval '24 hours'")

    assert len(conn.content_queries) == 2
    assert out["telegram"]["records"] == 12
    assert out["telegram"]["messages"] == 12
    assert out["telegram"]["media_items"] == 0
    assert out["telegram"]["media_stats_unavailable"] is True


@pytest.mark.asyncio
async def test_source_content_summary_can_skip_heavy_media_window(monkeypatch):
    async def no_beeper_subsources(conn, since_sql, before_sql=None):
        return {}

    monkeypatch.setattr(
        "src.dashboard.api._beeper_subsource_content_summary",
        no_beeper_subsources,
    )

    class FakeConn:
        def __init__(self):
            self.content_queries = []

        async def fetchval(self, query, *args, timeout=None):
            assert "information_schema.tables" in query
            return ["telegram_messages", "media_items"]

        async def fetch(self, query, *args, timeout=None):
            self.content_queries.append((query, timeout))
            assert "FROM media_items" not in query
            return [
                {
                    "source": "telegram",
                    "records": 12,
                    "messages": 12,
                    "media_items": 0,
                    "latest_record_at": datetime(2026, 7, 28, 1, 5, tzinfo=timezone.utc),
                    "latest_media_at": None,
                },
            ]

    conn = FakeConn()
    out = await _source_content_summary(
        conn,
        "now() - interval '24 hours'",
        include_media=False,
    )

    assert len(conn.content_queries) == 1
    assert out["telegram"]["records"] == 12
    assert out["telegram"]["messages"] == 12
    assert out["telegram"]["media_items"] == 0
    assert out["telegram"]["media_stats_unavailable"] is True


@pytest.mark.asyncio
async def test_source_matrix_section_uses_fresh_cache_without_awaiting():
    _SOURCE_MATRIX_SECTION_CACHE.clear()
    errors: list[dict] = []
    started = False

    async def returns(value):
        return value

    async def should_not_run():
        nonlocal started
        started = True
        raise AssertionError("cached section should not await fresh query")

    first = await _source_matrix_section(
        section="current_content",
        label="current content",
        errors=errors,
        fallback={},
        awaitable=returns({"instagram": {"records": 1}}),
        timeout=1,
        cache_key="test_current_content",
        cache_ttl=60,
    )
    second = await _source_matrix_section(
        section="current_content",
        label="current content",
        errors=errors,
        fallback={},
        awaitable=should_not_run(),
        timeout=1,
        cache_key="test_current_content",
        cache_ttl=60,
    )

    assert first == {"instagram": {"records": 1}}
    assert second == first
    assert errors == []
    assert started is False


@pytest.mark.asyncio
async def test_source_matrix_section_returns_stale_cache_on_timeout():
    _SOURCE_MATRIX_SECTION_CACHE.clear()
    errors: list[dict] = []

    async def returns(value):
        return value

    async def slow():
        await asyncio.sleep(0.05)
        return {"instagram": {"records": 99}}

    await _source_matrix_section(
        section="day_content",
        label="24h content",
        errors=errors,
        fallback={},
        awaitable=returns({"instagram": {"records": 12}}),
        timeout=1,
        cache_key="test_day_content",
        cache_ttl=0,
    )
    out = await _source_matrix_section(
        section="day_content",
        label="24h content",
        errors=errors,
        fallback={},
        awaitable=slow(),
        timeout=0.001,
        cache_key="test_day_content",
        cache_ttl=0,
        stale_ttl=60,
    )

    assert out == {"instagram": {"records": 12}}
    assert errors[-1]["section"] == "day_content"
    assert errors[-1]["error"] == "TimeoutError"
    assert errors[-1]["stale_cache"] is True
    assert errors[-1]["cache_age_seconds"] >= 0
