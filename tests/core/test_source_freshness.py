import pytest


class FakeConn:
    async def fetch(self, query: str):
        if "FROM source_health" in query:
            return [
                {
                    "source": "website",
                    "status": "degraded",
                    "last_error": "stale 123s - watchdog in cooldown",
                    "last_success_at": "2026-07-28T01:00:00Z",
                    "updated_at": "2026-07-28T01:05:00Z",
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
    assert by_source["website"]["source_health_last_success_at"] == "2026-07-28T01:00:00Z"
    assert by_source["website"]["source_health_updated_at"] == "2026-07-28T01:05:00Z"


@pytest.mark.asyncio
async def test_compute_liveness_ignores_stale_watchdog_marker_when_data_is_fresh(monkeypatch):
    from src.core import source_freshness

    class FreshWebsiteConn(FakeConn):
        async def fetchval(self, query: str, timeout: int = 8):
            if "website_pages" in query:
                return 120
            return 10

    monkeypatch.setattr(
        source_freshness,
        "FRESHNESS",
        [
            ("website", "SELECT extract(epoch FROM now()-max(collected_at)) FROM website_pages", 259200),
        ],
    )

    rows = await source_freshness.compute_liveness(FreshWebsiteConn())

    assert rows[0]["status"] == "live"
    assert "stale watchdog marker ignored" in rows[0]["detail"]
    assert rows[0]["source_health_status"] == "degraded"


@pytest.mark.asyncio
async def test_strava_liveness_counts_route_and_media_progress(monkeypatch):
    from src.core import source_freshness

    class StravaConn:
        async def fetch(self, query: str):
            if "FROM source_health" in query:
                return []
            raise AssertionError(query)

        async def fetchval(self, query: str, timeout: int = 8):
            assert "strava_gps_streams" in query
            assert "media_items WHERE source='strava'" in query
            return 30

    monkeypatch.setattr(
        source_freshness,
        "FRESHNESS",
        [("strava", source_freshness.STRAVA_PROGRESS_QUERY, 259200)],
    )

    rows = await source_freshness.compute_liveness(StravaConn())

    assert rows[0]["status"] == "live"
    assert rows[0]["age_seconds"] == 30
    assert rows[0]["freshness_basis"] == "newest Strava activity, GPS stream, or media row"
