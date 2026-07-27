import pytest


def test_format_backup_status_reports_first_dump_refreshing():
    from src.notifications.alerts import _format_backup_status

    msg = _format_backup_status({
        "status": "refreshing",
        "root": "/vault/backups/db",
        "latest_path": None,
        "latest_age_seconds": None,
        "latest_size_bytes": None,
        "backup_count": 0,
        "in_progress": True,
        "stale_in_progress_count": 0,
    })

    assert "No completed collector DB backup dump found" in msg
    assert "writing a replacement dump" in msg
    assert "Latest collector DB backup" not in msg


@pytest.mark.asyncio
async def test_notify_status_splits_rate_limit_from_auth_events(monkeypatch):
    from src.notifications import alerts

    sent: list[str] = []

    async def fake_send(text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(alerts.telegram, "send", fake_send)

    ok = await alerts.notify_status({
        "ok": False,
        "hourly_ingestion": {
            "totals": {
                "records": 10,
                "messages": 2,
                "files": 1,
                "rate_limits": 1,
                "access_errors": 1,
            },
            "sources": [
                {
                    "source": "instagram",
                    "records": 4,
                    "messages": 0,
                    "files": 1,
                    "rate_limits": 1,
                    "access_errors": 1,
                }
            ],
            "previous_complete_hour": {
                "totals": {
                    "records": 20,
                    "messages": 8,
                    "files": 3,
                    "rate_limits": 0,
                    "access_errors": 0,
                },
                "sources": [
                    {
                        "source": "telegram",
                        "records": 8,
                        "messages": 8,
                        "files": 1,
                        "rate_limits": 0,
                        "access_errors": 0,
                    }
                ],
            },
        },
        "rate_limit_events": [
            {
                "source": "instagram",
                "account": "bryanseah234",
                "scope": "profile_fetch",
                "status_code": 429,
                "count": 1,
            }
        ],
        "active_rate_limits": [
            {
                "service": "instagram_rate_limit",
                "seconds_remaining": 3600,
                "streak": 7,
            },
            {
                "service": "telegram",
                "account": "acct1",
                "scope": "flood_wait",
                "seconds_remaining": 120,
                "events": 1,
                "reason": "Telegram FloodWaitError",
            },
        ],
        "access_events": [
            {
                "source": "instagram",
                "account": "bryanseah234",
                "scope": "profile_fetch_playwright",
                "status_code": 401,
                "count": 1,
                "reason": "Playwright profile auth response",
            }
        ],
        "extension_hooks": [
            {
                "platform": "instagram",
                "age_seconds": 21,
                "extension_version": "1.21.8",
                "expected_extension_version": "1.21.24",
                "owner_count": 1,
                "probes_current_hour": 12,
                "samples_current_hour": 0,
                "probes_sent": 144,
                "samples_shipped": 0,
                "last_frame_age_seconds": 93,
            }
        ],
        "browser_ingest_events": [
            {
                "platform": "threads",
                "endpoint": "media",
                "requests": 2,
                "observed_count": 12,
                "stored_count": 3,
            },
            {
                "platform": "instagram",
                "endpoint": "profile",
                "requests": 1,
                "observed_count": 1,
                "stored_count": 1,
            },
        ],
        "degraded_sources": ["instagram"],
        "degraded_details": [
            {
                "source": "instagram",
                "status": "degraded",
                "age_seconds": 341_849,
                "stale_after_seconds": 172_800,
                "reason": "stale 341849s — watchdog active HTTP 429 cooldown 13263s left; not restarted",
            }
        ],
        "vault": {
            "root": "/vault",
            "available": True,
            "writable": True,
            "free_bytes": 1024,
            "artifacts_queued": 0,
            "artifacts_partial": 0,
            "sidecar_failures": 0,
            "counts_error": "TimeoutError",
        },
        "backups": {
            "status": "ok",
            "root": "/vault/backups/db",
            "latest_path": "/vault/backups/db/unifiedcollector_20260723_033005.dump",
            "latest_created_at": "2026-07-23T03:30:05",
            "latest_age_seconds": 7200,
            "latest_size_bytes": 3_500_000_000,
            "backup_count": 4,
            "in_progress": False,
            "in_progress_count": 0,
            "stale_in_progress_count": 1,
            "stale_in_progress_oldest_age_seconds": 90_000,
            "max_age_hours": 30,
            "error": None,
        },
    })

    assert ok is True
    msg = sent[0]
    assert "This is the partial clock-hour window" in msg
    assert "Recorded rate-limit events this hour: 1." in msg
    assert "Recorded login/access or other HTTP errors this hour: 1." in msg
    assert "<b>Previous complete hour</b>" in msg
    assert "Stored 20 source rows, including 8 chat messages and 3 media files" in msg
    assert "Telegram: 8 source rows, 1 media file" in msg
    assert "DLQ" not in msg
    assert "non-429" not in msg
    assert "Instagram: 4 source rows, 1 media file, 1 rate-limit event, 1 login/access or other HTTP error" in msg
    assert "Login/access or other HTTP errors this hour:" in msg
    assert "HTTP 401" in msg
    assert "Instagram: active cooldown for 60m after 7 instrumented rate-limit events." in msg
    assert "Telegram FloodWait throttles for acct1: active cooldown for 2m after 1 FloodWait event" in msg
    assert "<b>Chrome extension hooks</b>" in msg
    assert "Instagram: hook v1.21.8 last heartbeat 21s ago" in msg
    assert "this hour 12 probe frames and 0 sample frames" in msg
    assert "last decoded frame 1m ago" in msg
    assert "repo expects v1.21.24; reload the unpacked extension" in msg
    assert "<b>Browser extension ingest</b>" in msg
    assert "Threads media files: browser saw 12 items; stored 3; 2 POSTs this hour." in msg
    assert "Instagram profiles: browser saw 1 item; stored 1; 1 POST this hour." in msg
    assert "Degraded sources: Instagram" in msg
    assert "Why degraded:" in msg
    assert "newest row 4.0d ago; expected within 2.0d" in msg
    assert "active HTTP 429 cooldown" in msg
    assert "Writable at <code>/vault</code>" in msg
    assert "Artifact health counts timed out" in msg
    assert "<b>DB backups</b>" in msg
    assert "Latest collector DB backup is 2.0h old" in msg
    assert "4 dumps retained under <code>/vault/backups/db</code>" in msg
    assert "1 abandoned temp dump" in msg
