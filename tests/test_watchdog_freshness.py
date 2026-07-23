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
