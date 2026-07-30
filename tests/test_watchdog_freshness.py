import importlib
import time

import pytest


@pytest.mark.asyncio
async def test_watchdog_skips_stale_restart_during_active_429_cooldown(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(
        freshness,
        "CHECKS",
        {"instagram": ("SELECT 20", 10, ["unifiedcollector_collector_instagram"])},
    )
    monkeypatch.setattr(freshness, "_last_restart", {})
    monkeypatch.setattr(freshness, "_last_cooldown_stale_alert", {})

    restarted: list[str] = []
    degraded: list[tuple[str, float, bool, str | None]] = []
    notified: list[str] = []

    async def fake_restart(container: str) -> None:
        restarted.append(container)

    async def fake_mark_degraded(db, source: str, age: float, restarted_any: bool, detail: str | None = None) -> None:
        degraded.append((source, age, restarted_any, detail))

    async def fake_notify(text: str) -> None:
        notified.append(text)

    monkeypatch.setattr(freshness, "_restart", fake_restart)
    monkeypatch.setattr(freshness, "_mark_degraded", fake_mark_degraded)
    monkeypatch.setattr(freshness, "_notify", fake_notify)

    class FakeDB:
        async def fetchval(self, query: str):
            assert query == "SELECT 20"
            return 20

        async def fetchrow(self, query: str, *args):
            if "FROM service_cursors" in query:
                return {
                    "service": "instagram_rate_limit",
                    "last_processed_id": f"{time.time() + 3600}:12",
                }
            raise AssertionError(query)

    await freshness._tick(FakeDB())

    assert restarted == []
    assert len(degraded) == 1
    source, age, restarted_any, detail = degraded[0]
    assert source == "instagram"
    assert age == 20
    assert restarted_any is False
    assert detail and "active HTTP 429 cooldown" in detail
    assert "not restarted" in detail
    assert notified
    assert "stale but cooling down" in notified[0]


@pytest.mark.asyncio
async def test_watchdog_clears_stale_marker_after_source_recovers(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(
        freshness,
        "CHECKS",
        {"website": ("SELECT 5", 10, ["unifiedcollector_collector_website"])},
    )

    restarted: list[str] = []
    degraded: list[tuple[str, float, bool, str | None]] = []
    executed: list[tuple[str, tuple]] = []

    async def fake_restart(container: str) -> None:
        restarted.append(container)

    async def fake_mark_degraded(db, source: str, age: float, restarted_any: bool, detail: str | None = None) -> None:
        degraded.append((source, age, restarted_any, detail))

    monkeypatch.setattr(freshness, "_restart", fake_restart)
    monkeypatch.setattr(freshness, "_mark_degraded", fake_mark_degraded)

    class FakeDB:
        async def fetchval(self, query: str):
            assert query == "SELECT 5"
            return 5

        async def execute(self, query: str, *args):
            executed.append((query, args))

    await freshness._tick(FakeDB())

    assert restarted == []
    assert degraded == []
    assert len(executed) == 1
    query, args = executed[0]
    assert "UPDATE source_health" in query
    assert "LIKE 'stale %watchdog%'" in query
    assert args == ("website",)


@pytest.mark.asyncio
async def test_watchdog_does_not_restart_whatsapp_waiting_for_qr(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(
        freshness,
        "CHECKS",
        {"whatsapp": ("SELECT 20", 10, ["unifiedcollector_wa_bridge_1", "unifiedcollector_wa_bridge_2"])},
    )

    restarted: list[str] = []
    degraded: list[tuple[str, float, bool, str | None]] = []

    async def fake_restart(container: str) -> None:
        restarted.append(container)

    async def fake_mark_degraded(db, source: str, age: float, restarted_any: bool, detail: str | None = None) -> None:
        degraded.append((source, age, restarted_any, detail))

    async def fake_whatsapp_pairing_needed() -> str:
        return "waiting for QR pairing; not restarted"

    monkeypatch.setattr(freshness, "_restart", fake_restart)
    monkeypatch.setattr(freshness, "_mark_degraded", fake_mark_degraded)
    monkeypatch.setattr(freshness, "_whatsapp_pairing_needed", fake_whatsapp_pairing_needed)

    class FakeDB:
        async def fetchval(self, query: str):
            assert query == "SELECT 20"
            return 20

    await freshness._tick(FakeDB())

    assert restarted == []
    assert degraded == [("whatsapp", 20, False, "waiting for QR pairing; not restarted")]


@pytest.mark.asyncio
async def test_watchdog_clears_dlq_marker_after_queue_drains(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)

    executed: list[tuple[str, tuple]] = []

    class FakeDB:
        async def fetch(self, query: str):
            if "FROM dead_letter_queue WHERE status='pending' GROUP BY source" in query:
                return []
            if "FROM source_health" in query and "dlq backlog:%watchdog%" in query.lower():
                return [{"source": "threads"}]
            raise AssertionError(query)

        async def execute(self, query: str, *args):
            executed.append((query, args))

    await freshness._dlq_tick(FakeDB())

    assert len(executed) == 1
    query, args = executed[0]
    assert "UPDATE source_health" in query
    assert "LIKE 'dlq backlog:%watchdog%'" in query
    assert args == ("threads",)
