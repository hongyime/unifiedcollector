import pytest

import os

os.environ.setdefault("DATABASE_URL", "postgresql://collector:x@127.0.0.1:5999/unifiedcollector")

import pytest

from src.watchdog import freshness


class _FakeDB:
    """Minimal asyncpg-conn stub: paused-source lookup + captured degraded marks."""

    def __init__(self, paused_sources, liveness_rows):
        self._paused = paused_sources
        self._liveness = liveness_rows
        self.degraded_marked = []
        self.notified = []

    async def fetch(self, sql, *args):
        if "collection_schedules" in sql and "enabled = false" in sql:
            watched = set(args[0]) if args else set()
            return [{"source": s} for s in self._paused if s in watched]
        return []


@pytest.mark.asyncio
async def test_browser_tick_skips_rotator_paused_source(monkeypatch):
    # x is rotated OFF (paused); facebook is genuinely stalled and NOT paused.
    monkeypatch.setattr(
        freshness, "BROWSER_SOURCE_WATCH_SOURCES", {"x", "facebook"}, raising=False
    )
    liveness = [
        {"source": "x", "status": "degraded", "browser_heartbeat_age_seconds": 9999,
         "browser_content_stale": True, "detail": "stale"},
        {"source": "facebook", "status": "degraded", "browser_heartbeat_age_seconds": 9999,
         "browser_content_stale": True, "detail": "stale"},
    ]

    async def fake_compute_liveness(_db):
        return liveness

    import src.core.source_freshness as sf
    monkeypatch.setattr(sf, "compute_liveness", fake_compute_liveness)

    marked = []
    notified = []

    async def fake_mark(db, source, detail):
        marked.append(source)

    async def fake_notify(text):
        notified.append(text)

    async def fake_mark_running(db, source):
        return None

    monkeypatch.setattr(freshness, "_mark_degraded_browser_source", fake_mark)
    monkeypatch.setattr(freshness, "_notify", fake_notify)
    monkeypatch.setattr(freshness, "_mark_running_if_browser_watchdog", fake_mark_running)
    monkeypatch.setattr(freshness, "_mark_running_if_stale_watchdog", fake_mark_running)
    monkeypatch.setattr(freshness, "_last_browser_source_alert", {}, raising=False)

    db = _FakeDB(paused_sources={"x"}, liveness_rows=liveness)
    await freshness._browser_source_tick(db)

    # x is rotator-paused -> no degraded mark, no alert. facebook still alerts.
    assert "x" not in marked
    assert "facebook" in marked
    assert any("facebook" in n for n in notified)
    assert not any("x browser collection stalled" in n for n in notified)
