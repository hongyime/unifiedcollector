from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors import airbnb as airbnb_mod
from src.collectors.airbnb import AirbnbCollector
from src.collectors.native_platform_scrape import _remaining_queue_capacity, seed_native_targets


@pytest.mark.asyncio
async def test_probe_profile_uses_numeric_airbnb_profile_route(monkeypatch):
    response = MagicMock(
        status_code=200,
        text=(
            '{"first_name":"Ada","last_name":"Lovelace",'
            '"picture_url":"https://cdn.example/avatar.jpg",'
            '"created_at":"2020-01-02","is_superhost":true}'
        ),
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    @asynccontextmanager
    async def fake_client(*args, **kwargs):
        yield client

    monkeypatch.setattr(airbnb_mod.httpx, "AsyncClient", fake_client)
    collector = AirbnbCollector()

    result = await collector.probe_profile("350595032")

    assert client.get.await_args.args[0] == "https://www.airbnb.com/users/show/350595032"
    assert result.exists is True
    assert result.fields["display_name"] == "Ada Lovelace"


@pytest.mark.asyncio
async def test_probe_profile_treats_unparsed_200_as_error_not_404(monkeypatch):
    response = MagicMock(status_code=200, text="<html>App shell</html>")
    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    @asynccontextmanager
    async def fake_client(*args, **kwargs):
        yield client

    monkeypatch.setattr(airbnb_mod.httpx, "AsyncClient", fake_client)
    result = await AirbnbCollector().probe_profile("350595032")

    assert result.exists is False
    assert result.error == "profile_payload_unavailable"


def test_airbnb_pdp_parser_extracts_public_listing_and_host_fields():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": [
                        {"sectionId": "TITLE_DEFAULT", "section": {"title": "Harbor Loft"}},
                        {"sectionId": "LOCATION_DEFAULT", "section": {"subtitle": "Singapore"}},
                        {"sectionId": "REVIEWS_DEFAULT", "section": {"overallRating": 4.92, "reviewsCount": 17}},
                        {
                            "sectionId": "MEET_YOUR_HOST",
                            "section": {
                                "cardData": {
                                    "userId": "5821",
                                    "name": "Public Host",
                                    "pictureUrl": "https://a0.muscache.com/host.jpg",
                                    "rating": 4.98,
                                    "reviewsCount": 82,
                                }
                            },
                        },
                    ]
                }
            }
        }
    }

    result = airbnb_mod._parse_airbnb_listing(payload, "123456")

    assert result is not None
    assert result["title"] == "Harbor Loft"
    assert result["location"] == "Singapore"
    assert result["rating"] == 4.92
    assert result["reviews_count"] == 17
    assert result["host_id"] == "5821"
    assert result["host_name"] == "Public Host"
    assert result["host_profile_url"] == "https://www.airbnb.com/users/show/5821"


def test_airbnb_review_parser_caps_to_ten_and_extracts_public_fields():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "reviews": {
                        "reviews": [
                            {
                                "id": "review-1",
                                "createdAt": "2025-03-04T00:00:00Z",
                                "rating": 5,
                                "commentV2": {"localizedReview": "Great stay"},
                                "language": "en",
                                "reviewer": {"id": "reviewer-9"},
                            }
                        ]
                    }
                }
            }
        }
    }

    result = airbnb_mod._parse_airbnb_reviews(payload, "123456")

    assert result == [{
        "review_id": "review-1",
        "listing_id": "123456",
        "reviewer_id": "reviewer-9",
        "review_date": "2025-03-04T00:00:00Z",
        "rating": 5.0,
        "comments": "Great stay",
        "language": "en",
    }]


def test_airbnb_native_targets_accept_only_https_public_listing_or_host_routes():
    assert AirbnbCollector._target_id("https://www.airbnb.com/rooms/123") == ("listing", "123")
    assert AirbnbCollector._target_id("https://www.airbnb.com.sg/users/show/456") == ("host", "456")
    assert AirbnbCollector._target_id("http://www.airbnb.com/rooms/123") is None
    assert AirbnbCollector._target_id("https://evil.example/rooms/123") is None
    assert AirbnbCollector._target_id("https://www.airbnb.com/rooms/abc") is None


@pytest.mark.parametrize(("active_count", "expected_limit", "expected_seeded"), [(10, 0, 0), (7, 3, 3)])
@pytest.mark.asyncio
async def test_airbnb_listing_seed_respects_remaining_active_queue_capacity(
    active_count, expected_limit, expected_seeded,
):
    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Connection:
        def __init__(self):
            self.listing_limit = None
            self.seeded_count = 0

        def transaction(self):
            return Transaction()

        async def execute(self, query, *args):
            if "'airbnb_listings'" in query:
                self.listing_limit = args[0]
                self.seeded_count = min(5, args[0])

        async def fetchval(self, query):
            assert "status IN ('pending','claimed')" in query
            assert "source='airbnb_listings'" in query
            return active_count

    class Pool:
        def __init__(self):
            self.conn = Connection()

        @asynccontextmanager
        async def acquire(self):
            yield self.conn

    pool = Pool()

    await seed_native_targets(pool, "airbnb")

    assert pool.conn.listing_limit == expected_limit
    assert pool.conn.seeded_count == expected_seeded
    assert _remaining_queue_capacity(14) == 0
