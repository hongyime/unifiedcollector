import pytest


@pytest.mark.asyncio
async def test_notify_status_splits_429s_from_auth_errors(monkeypatch):
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
        },
        "active_rate_limits": [
            {
                "service": "instagram_rate_limit",
                "seconds_remaining": 3600,
                "streak": 7,
            }
        ],
        "rate_limit_events": [
            {
                "source": "instagram",
                "account": "bryanseah234",
                "scope": "profile_fetch",
                "status_code": 429,
                "count": 1,
            }
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
                "owner_count": 1,
                "probes_current_hour": 12,
                "samples_current_hour": 0,
                "probes_sent": 144,
                "samples_shipped": 0,
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
    assert "Recorded HTTP 429 events this hour: 1." in msg
    assert "Recorded auth/access HTTP errors this hour: 1." in msg
    assert "Instagram: 4 source rows, 1 media file, 1 HTTP 429 event, 1 auth/access HTTP error" in msg
    assert "Session/auth HTTP failures this hour:" in msg
    assert "HTTP 401" in msg
    assert "<b>Chrome extension hooks</b>" in msg
    assert "Instagram: hook v1.21.8 last heartbeat 21s ago" in msg
    assert "this hour 12 probe frames and 0 sample frames" in msg
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
