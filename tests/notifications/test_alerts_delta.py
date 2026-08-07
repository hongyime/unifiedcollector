"""Tests for the 15-min delta status update (Feature 2).

Focused on the message builder in alerts.notify_status_delta and the delta
snapshot shape produced by Scheduler._build_status_delta. The DB path is
exercised via a fake connection so no Postgres is needed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest


# -- notify_status_delta message shape -----------------------------------

@pytest.mark.asyncio
async def test_notify_status_delta_happy_path(monkeypatch):
    from src.notifications import alerts

    sent: list[str] = []

    async def fake_send(text: str, parse_mode: str = "HTML") -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(alerts.telegram, "send", fake_send)

    ok = await alerts.notify_status_delta({
        "window_seconds": 900,
        "totals": {"posts": 12, "media": 40, "messages": 7, "cooldowns": 0},
        "per_source": {
            "instagram": {"posts": 6, "media": 20, "messages": 0},
            "tiktok": {"posts": 4, "media": 8, "messages": 0},
            "telegram": {"posts": 0, "media": 0, "messages": 7},
            "lemon8": {"posts": 2, "media": 12, "messages": 0},
        },
        "new_cooldowns": [],
        "new_dead_sources": [],
        "extension_hooks": [
            {"platform": "instagram", "age_seconds": 30, "extension_version": "1.23.33"},
            {"platform": "tiktok", "age_seconds": 45, "extension_version": "1.23.33"},
        ],
    })

    assert ok is True
    assert len(sent) == 1
    body = sent[0]
    # One compact message, not a multi-section digest.
    assert body.count("<b>UnifiedCollector delta</b>") == 1
    assert "12 new posts" in body
    assert "40 new media files" in body
    assert "7 new messages" in body
    # Per-source detail is present.
    assert "Instagram" in body and "20 media files" in body
    assert "Telegram" in body and "7 messages" in body
    # Extension summary is a single line.
    assert "Extension" in body
    # No 15-min-specific breakdown of the hourly digest.
    assert "Current hour" not in body
    assert "Top activity this hour" not in body


@pytest.mark.asyncio
async def test_notify_status_delta_flags_new_cooldowns(monkeypatch):
    from src.notifications import alerts

    sent: list[str] = []

    async def fake_send(text: str, parse_mode: str = "HTML") -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(alerts.telegram, "send", fake_send)

    await alerts.notify_status_delta({
        "window_seconds": 900,
        "totals": {"posts": 0, "media": 0, "messages": 0, "cooldowns": 1},
        "per_source": {},
        "new_cooldowns": [
            {"service": "instagram", "scope": "feed", "account": "hongyime",
             "seconds_remaining": 1800, "events": 2, "reason": "http 429"},
        ],
        "new_dead_sources": [],
        "extension_hooks": [],
    })
    body = sent[0]
    # Warning icon when cooldowns exist.
    assert body.startswith("⚠️")
    assert "New cooldowns" in body
    assert "Instagram" in body
    assert "30m" in body  # 1800s -> 30m


@pytest.mark.asyncio
async def test_notify_status_delta_reports_new_dead(monkeypatch):
    from src.notifications import alerts

    sent: list[str] = []

    async def fake_send(text: str, parse_mode: str = "HTML") -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(alerts.telegram, "send", fake_send)

    await alerts.notify_status_delta({
        "window_seconds": 900,
        "totals": {"posts": 0, "media": 0, "messages": 0, "cooldowns": 0},
        "per_source": {},
        "new_cooldowns": [],
        "new_dead_sources": ["whatsapp"],
        "extension_hooks": [],
    })
    body = sent[0]
    assert body.startswith("⚠️")
    assert "New dead sources" in body
    assert "WhatsApp" in body


@pytest.mark.asyncio
async def test_notify_status_delta_handles_db_error(monkeypatch):
    from src.notifications import alerts

    sent: list[str] = []

    async def fake_send(text: str, parse_mode: str = "HTML") -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(alerts.telegram, "send", fake_send)

    ok = await alerts.notify_status_delta({"error": "connection refused"})
    assert ok is True
    assert "DB unreachable" in sent[0]
    assert "connection refused" in sent[0]


@pytest.mark.asyncio
async def test_notify_status_delta_empty_tick(monkeypatch):
    """No new activity should still send a single (small) message."""
    from src.notifications import alerts

    sent: list[str] = []

    async def fake_send(text: str, parse_mode: str = "HTML") -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(alerts.telegram, "send", fake_send)

    await alerts.notify_status_delta({
        "window_seconds": 900,
        "totals": {"posts": 0, "media": 0, "messages": 0, "cooldowns": 0},
        "per_source": {},
        "new_cooldowns": [],
        "new_dead_sources": [],
        "extension_hooks": [],
    })
    body = sent[0]
    assert body.startswith("✅")
    assert "0 new posts" in body
    assert "0 new media files" in body
    assert "0 new messages" in body


def test_delta_row_no_activity_line():
    from src.notifications.alerts import _format_delta_row

    line = _format_delta_row({"source": "instagram"})
    assert "Instagram" in line
    assert "no new activity" in line


def test_extension_summary_marks_stale():
    from src.notifications.alerts import _extension_summary_line

    line = _extension_summary_line([
        {"platform": "instagram", "age_seconds": 60, "extension_version": "1.23.33"},
        {"platform": "tiktok", "age_seconds": 7200, "extension_version": "1.23.33"},
    ])
    assert "Extension" in line
    assert "heartbeating on Instagram" in line
    assert "stale on TikTok" in line


# -- _build_status_delta shape (Scheduler) -------------------------------

class FakeConn:
    """Minimal fake asyncpg Connection for _build_status_delta.

    Answers a small fixed set of queries. The scheduler code catches
    exceptions and degrades gracefully, so any query we don't recognize
    can safely raise and be swallowed.
    """

    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self.cursor_row = None

    async def fetchrow(self, query: str, *args):
        self.executed.append(("fetchrow", (query, args)))
        if "service_cursors" in query:
            return self.cursor_row
        return None

    async def fetchval(self, query: str, *args, timeout: float = 0):
        self.executed.append(("fetchval", (query, args)))
        if "to_regclass('dm_hook_heartbeat')" in query:
            return "dm_hook_heartbeat"
        if "to_regclass('operational_events')" in query:
            return "operational_events"
        if "FROM telegram_messages" in query:
            return 3
        if "FROM whatsapp_messages" in query:
            return 5
        if "FROM beeper_shadow_messages" in query:
            return 1
        if "FROM instagram_posts" in query:
            return 4
        if "FROM tiktok_posts" in query:
            return 2
        return 0

    async def fetch(self, query: str, *args, timeout: float = 0):
        self.executed.append(("fetch", (query, args)))
        if "FROM media_items" in query:
            return [
                {"source": "instagram", "n": 10},
                {"source": "tiktok", "n": 3},
            ]
        if "FROM rate_limit_events" in query:
            # Simulate one new cooldown 25 minutes remaining.
            return [{
                "source": "instagram", "account": "acct", "scope": "feed",
                "started_at": datetime.now(timezone.utc),
                "seconds_remaining": 1500,
                "events": 2, "reason": "http 429",
            }]
        if "FROM operational_events" in query:
            return [{"source": "whatsapp"}]
        if "FROM dm_hook_heartbeat" in query:
            return [{"platform": "instagram", "age_seconds": 20,
                     "extension_version": "1.23.33"}]
        return []

    async def execute(self, query: str, *args):
        self.executed.append(("execute", (query, args)))
        return "UPDATE 1"


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _AcquireCtx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

        return _AcquireCtx()


@pytest.mark.asyncio
async def test_build_status_delta_snapshot_shape():
    from src.scheduler import Scheduler

    conn = FakeConn()
    conn.cursor_row = {"last_processed_at": datetime.now(timezone.utc) - timedelta(minutes=30)}
    scheduler = Scheduler.__new__(Scheduler)  # bypass __init__
    scheduler.pool = FakePool(conn)

    snapshot = await scheduler._build_status_delta(15)

    assert snapshot is not None
    assert "window_seconds" in snapshot
    # Totals include posts + media + messages across sources.
    totals = snapshot["totals"]
    assert totals["posts"] >= 4 + 2  # instagram + tiktok posts
    assert totals["media"] == 10 + 3
    assert totals["messages"] == 3 + 5 + 1
    assert totals["cooldowns"] == 1
    # Per-source has entries for each contributor.
    per_source = snapshot["per_source"]
    assert per_source["instagram"]["media"] == 10
    assert per_source["telegram"]["messages"] == 3
    assert per_source["whatsapp"]["messages"] == 5
    assert per_source["beeper"]["messages"] == 1
    # New cooldowns / dead sources / extension hooks all populated.
    assert len(snapshot["new_cooldowns"]) == 1
    assert snapshot["new_cooldowns"][0]["service"] == "instagram"
    assert snapshot["new_dead_sources"] == ["whatsapp"]
    assert snapshot["extension_hooks"] == [
        {"platform": "instagram", "age_seconds": 20, "extension_version": "1.23.33"},
    ]

    # Cursor persists after building.
    executed_kinds = [call[0] for call in conn.executed]
    assert "execute" in executed_kinds
    upsert_call = [c for c in conn.executed if c[0] == "execute"][0]
    assert "service_cursors" in upsert_call[1][0]
    assert "notify_status_delta" in upsert_call[1][0]


@pytest.mark.asyncio
async def test_build_status_delta_skips_when_cursor_recent():
    """A persisted cursor <90% of interval ago means another instance ticked."""
    from src.scheduler import Scheduler

    conn = FakeConn()
    conn.cursor_row = {
        "last_processed_at": datetime.now(timezone.utc) - timedelta(minutes=2),
    }
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.pool = FakePool(conn)

    snapshot = await scheduler._build_status_delta(15)
    assert snapshot is None
    # No cursor upsert since we did not tick.
    upserts = [c for c in conn.executed if c[0] == "execute"]
    assert upserts == []


@pytest.mark.asyncio
async def test_build_status_delta_seeds_first_run():
    """First-ever run (no cursor row) still returns a snapshot."""
    from src.scheduler import Scheduler

    conn = FakeConn()
    conn.cursor_row = None
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.pool = FakePool(conn)

    snapshot = await scheduler._build_status_delta(15)
    assert snapshot is not None
    # Cursor is persisted so a restart won't double-send.
    upserts = [c for c in conn.executed if c[0] == "execute" and "service_cursors" in c[1][0]]
    assert upserts, "expected an upsert into service_cursors on first run"


# -- Regression: existing notify_status is unchanged ----------------------

@pytest.mark.asyncio
async def test_existing_notify_status_still_uses_send_many(monkeypatch):
    """Feature 2 is additive; the hourly digest must still call send_many."""
    from src.notifications import alerts

    calls: dict[str, int] = {"send": 0, "send_many": 0}

    async def fake_send(text: str, parse_mode: str = "HTML") -> bool:
        calls["send"] += 1
        return True

    async def fake_send_many(messages: list[str]) -> bool:
        calls["send_many"] += 1
        return True

    monkeypatch.setattr(alerts.telegram, "send", fake_send)
    monkeypatch.setattr(alerts.telegram, "send_many", fake_send_many)

    await alerts.notify_status({
        "ok": True,
        "hourly_ingestion": {
            "totals": {"records": 1, "messages": 0, "files": 0,
                       "rate_limits": 0, "access_errors": 0},
            "sources": [],
        },
    })
    assert calls["send_many"] == 1
