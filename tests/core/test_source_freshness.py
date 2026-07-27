import pytest


class FakeConn:
    async def fetch(self, query: str):
        if "FROM source_health" in query:
            return [
                {
                    "source": "website",
                    "status": "degraded",
                    "last_error": "stale 123s - watchdog in cooldown",
                }
            ]
        raise AssertionError(query)

    async def fetchval(self, query: str, timeout: int = 8):
        if "telegram_messages" in query:
            return 10
        if "website_pages" in query:
            return 400_000
        return None


@pytest.mark.asyncio
async def test_compute_liveness_includes_collection_mode_basis_and_reason(monkeypatch):
    from src.core import source_freshness

    monkeypatch.setattr(
        source_freshness,
        "FRESHNESS",
        [
            ("telegram", "SELECT extract(epoch FROM now()-max(collected_at)) FROM telegram_messages", 7200),
            ("website", "SELECT extract(epoch FROM now()-max(collected_at)) FROM website_pages", 259200),
        ],
    )

    rows = await source_freshness.compute_liveness(FakeConn())
    by_source = {row["source"]: row for row in rows}

    assert by_source["telegram"]["status"] == "live"
    assert by_source["telegram"]["collection_mode"] == "messaging"
    assert by_source["telegram"]["freshness_basis"] == "telegram_messages.collected_at"
    assert "inside the freshness window" in by_source["telegram"]["detail"]

    assert by_source["website"]["status"] == "stale"
    assert by_source["website"]["source_health_status"] == "degraded"
    assert by_source["website"]["source_health_error"] == "stale 123s - watchdog in cooldown"
