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

    async def _noop_sleep(_secs):
        pass

    monkeypatch.setattr(alerts.telegram.asyncio, "sleep", _noop_sleep)

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
        "operational_events": [
            {
                "source": "telegram",
                "event_type": "self_heal_restart",
                "severity": "warning",
                "summary": "telegram: fatal log pattern flooded: wrong session ID",
                "metadata": {"hit_count": 25},
                "age_seconds": 30,
                "resolved_by_success": True,
                "last_success_age_seconds": 5,
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
        "browser_media_diagnostics": [
            {"platform": "facebook", "outcome": "tiny_thumbnail", "candidates": 12, "needs_revisit": 0},
            {"platform": "x", "outcome": "browser_fetch_failed", "candidates": 2, "needs_revisit": 2},
        ],
        "browser_media_revisit_queue": [
            {
                "platform": "x",
                "due": 2,
                "claimed": 1,
                "stale_claimed": 0,
                "pending": 3,
                "failed": 1,
                "unavailable": 0,
                "completed": 4,
            }
        ],
        "tiktok_browser_media_diagnostics": [
            {"outcome": "short_lived_url", "candidates": 3, "needs_revisit": 3},
            {"outcome": "tiny_thumbnail", "candidates": 8, "needs_revisit": 0},
        ],
        "tiktok_browser_revisit_queue": {
            "due": 2,
            "claimed": 1,
            "stale_claimed": 1,
            "pending": 4,
            "failed": 1,
            "unavailable": 0,
            "completed": 7,
        },
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
            "artifacts_quarantined": 0,
            "artifacts_missing_sidecar": 0,
            "artifacts_missing_sidecar_estimated": True,
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
    assert len(sent) >= 3, f"expected multiple section messages, got {len(sent)}"
    msg = "\n".join(sent)
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
    assert "<b>Recent self-heals and operational events</b>" in msg
    assert "Resolved history: 1 older operational event for Telegram already recovered" in msg
    assert "newest 30s ago; latest successful collection 5s ago" in msg
    assert "telegram: fatal log pattern flooded: wrong session ID" not in msg
    assert "<b>Chrome extension hooks</b>" in msg
    assert "Instagram: hook v1.21.8 last heartbeat 21s ago" in msg
    assert "this hour 12 probe frames and 0 sample frames" in msg
    assert "last decoded frame 1m ago" in msg
    assert "repo expects v1.21.24; reload the unpacked extension" in msg
    assert "<b>Browser extension ingest</b>" in msg
    assert "Threads media files: browser saw 12 items; stored 3; 2 POSTs this hour." in msg
    assert "Instagram profiles: browser saw 1 item; stored 1; 1 POST this hour." in msg
    assert "<b>Browser media diagnosis</b>" in msg
    assert "Facebook: tiny thumbnail/avatar rejected: 12." in msg
    assert "Twitter / X: browser fetch failed: 2; 2 candidates need detail revisit." in msg
    assert "<b>Browser media detail follow-up</b>" in msg
    assert "Twitter / X detail revisit queue: 2 due now, 1 claimed by browser" in msg
    assert "<b>TikTok media follow-up</b>" in msg
    assert "short-lived video URL queued for browser revisit: 3" in msg
    assert "TikTok detail revisit queue: 2 due now, 1 claimed by browser" in msg
    assert "1 stale claimed ready to reclaim" in msg
    assert "Degraded sources: Instagram" in msg
    assert "Why degraded:" in msg
    assert "newest row 4.0d ago; expected within 2.0d" in msg
    assert "active HTTP 429 cooldown" in msg
    assert "Writable at <code>/vault</code>" in msg
    assert "Artifact health counts partially timed out" in msg
    assert "<b>DB backups</b>" in msg
    assert "Latest collector DB backup is 2.0h old" in msg
    assert "4 dumps retained under <code>/vault/backups/db</code>" in msg
    assert "1 abandoned temp dump" in msg




@pytest.mark.asyncio
async def test_notify_status_emits_multiple_categorised_messages(monkeypatch):
    """A rich snapshot should fan out into >=3 non-empty section messages."""
    from src.notifications import alerts

    sent: list[str] = []
    send_many_calls: list[list[str]] = []

    async def fake_send(text: str) -> bool:
        sent.append(text)
        return True

    async def fake_send_many(messages: list[str]) -> bool:
        send_many_calls.append(list(messages))
        # Forward each non-empty entry through the patched send() so downstream
        # per-message assertions still see the individual pieces.
        ok = True
        for m in messages:
            if not m:
                continue
            ok = bool(await fake_send(m)) and ok
        return ok

    monkeypatch.setattr(alerts.telegram, "send", fake_send)
    monkeypatch.setattr(alerts.telegram, "send_many", fake_send_many)

    ok = await alerts.notify_status({
        "ok": True,
        "hourly_ingestion": {
            "totals": {"records": 5, "messages": 1, "files": 1, "rate_limits": 0, "access_errors": 0},
            "sources": [{"source": "instagram", "records": 5, "files": 1}],
        },
        "backups": {"status": "ok", "root": "/vault/backups/db", "latest_path": "/x.dump",
                     "latest_age_seconds": 60, "latest_size_bytes": 1_000_000, "backup_count": 1,
                     "in_progress": False, "max_age_hours": 30},
        "dead_sources": ["strava"],
    })

    assert ok is True
    # send_many called exactly once, with a list of at least three separate
    # category messages (header + rate-limits + current-hour + backups + ...).
    assert len(send_many_calls) == 1
    messages = send_many_calls[0]
    assert len(messages) >= 3, f"expected >=3 category messages, got {len(messages)}: {messages!r}"
    # Every emitted section starts with a bold header so it stands alone.
    assert all("<b>" in m for m in messages if m), messages
    # And the fan-out actually reaches send() once per section.
    assert len(sent) == len([m for m in messages if m])


@pytest.mark.asyncio
async def test_notify_status_omits_empty_sections(monkeypatch):
    """Minimal snapshot => only the always-on sections (header + rate limits)."""
    from src.notifications import alerts

    send_many_calls: list[list[str]] = []

    async def fake_send_many(messages: list[str]) -> bool:
        send_many_calls.append(list(messages))
        return True

    async def fake_send(_text: str) -> bool:  # should NOT be reached on the main path
        raise AssertionError("send() must not be used when send_many is available")

    monkeypatch.setattr(alerts.telegram, "send_many", fake_send_many)
    monkeypatch.setattr(alerts.telegram, "send", fake_send)

    ok = await alerts.notify_status({"ok": True})

    assert ok is True
    assert len(send_many_calls) == 1
    messages = send_many_calls[0]
    # Header (always) + Rate limits fallback (always) = exactly 2 messages.
    assert len(messages) == 2, f"expected exactly 2 sections, got {len(messages)}: {messages!r}"
    assert "UnifiedCollector hourly status" in messages[0]
    assert "<b>Rate limits, cooldowns, and sessions</b>" in messages[1]
    assert "No recorded rate-limit events" in messages[1]


@pytest.mark.asyncio
async def test_notify_status_error_snapshot_uses_single_send(monkeypatch):
    """The DB-unreachable early-exit branch must still be a single send()."""
    from src.notifications import alerts

    sent: list[str] = []
    send_many_called = False

    async def fake_send(text: str) -> bool:
        sent.append(text)
        return True

    async def fake_send_many(_messages):
        nonlocal send_many_called
        send_many_called = True
        return True

    monkeypatch.setattr(alerts.telegram, "send", fake_send)
    monkeypatch.setattr(alerts.telegram, "send_many", fake_send_many)

    ok = await alerts.notify_status({"error": "connection refused"})

    assert ok is True
    assert send_many_called is False
    assert len(sent) == 1
    assert "DB unreachable" in sent[0]
    assert "connection refused" in sent[0]


def test_section_builders_are_callable_and_pure():
    """Sanity check: every _section_* helper is importable and returns list|None."""
    from src.notifications import alerts

    builders = [
        alerts._section_header,
        alerts._section_current_hour,
        alerts._section_top_activity,
        alerts._section_previous_hour,
        alerts._section_rate_limits,
        alerts._section_operational,
        alerts._section_vault,
        alerts._section_backups,
        alerts._section_realtime_freshness,
        alerts._section_extension_hooks,
        alerts._section_browser_ingest,
        alerts._section_browser_content_gaps,
        alerts._section_browser_media_diagnosis,
        alerts._section_browser_media_revisit,
        alerts._section_tiktok_media,
        alerts._section_x_health,
        alerts._section_browser_api_freshness,
        alerts._section_backfill_state,
        alerts._section_dead_degraded,
    ]
    for fn in builders:
        result = fn({})
        assert result is None or isinstance(result, list), (fn.__name__, result)
        if isinstance(result, list):
            assert all(isinstance(x, str) for x in result), (fn.__name__, result)
    # Header and rate-limits are always emitted, even on an empty snapshot.
    assert alerts._section_header({}) is not None
    assert alerts._section_rate_limits({}) is not None
