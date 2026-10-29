from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors import airbnb as airbnb_mod
from src.collectors.airbnb import AirbnbCollector
from src.collectors.native_platform_scrape import (
    _FOLLOWED_AIRBNB_PROFILE_URLS_SQL,
    _remaining_queue_capacity,
    seed_native_targets,
)


def test_airbnb_error_code_keeps_safe_runtime_codes_without_leaking_messages():
    assert airbnb_mod._airbnb_error_code(RuntimeError("graphql_query_failed")) == "graphql_query_failed"
    assert airbnb_mod._airbnb_error_code(RuntimeError("https://example.test/?token=secret")) == "runtimeerror"
    assert airbnb_mod._airbnb_error_code(airbnb_mod._AirbnbPublicHttpError(429)) == "public_http_429"


@pytest.mark.asyncio
async def test_airbnb_public_api_key_follows_allowlisted_domain_handoff(monkeypatch):
    monkeypatch.delenv("AIRBNB_PUBLIC_API_KEY", raising=False)
    homepage = MagicMock(
        status_code=200,
        headers={},
        url="https://www.airbnb.com/",
        text=(
            '<form action="https://www.airbnb.com.sg/">'
            '<input name="payload" value="public-handoff-payload"></form>'
        ),
    )
    redirect = MagicMock(
        status_code=302,
        headers={"Location": "https://www.airbnb.com.sg/"},
        url="https://www.airbnb.com.sg/",
        text="",
    )
    landing_page = MagicMock(
        status_code=200,
        headers={},
        url="https://www.airbnb.com.sg/",
        text='{"api_config":{"key":"public-test-key"}}',
    )
    client = MagicMock()
    client.get = AsyncMock(side_effect=[homepage, landing_page])
    client.post = AsyncMock(return_value=redirect)
    monkeypatch.setattr(airbnb_mod, "_AIRBNB_API_KEY_CACHE", None)
    monkeypatch.setattr(airbnb_mod, "_AIRBNB_REGION_CACHE", None)

    key, base_url = await airbnb_mod._resolve_public_api_key(client)

    assert key == "public-test-key"
    assert base_url == "https://www.airbnb.com.sg"
    client.post.assert_awaited_once()
    assert client.post.await_args.args[0] == "https://www.airbnb.com.sg/"
    assert client.post.await_args.kwargs["data"] == {
        "version": "1", "payload": "public-handoff-payload",
    }


@pytest.mark.asyncio
async def test_configured_airbnb_api_key_still_follows_regional_handoff(monkeypatch):
    homepage = MagicMock(
        status_code=200,
        headers={},
        url="https://www.airbnb.com/",
        text=(
            '<form action="https://www.airbnb.com.sg/">'
            '<input name="payload" value="public-handoff-payload"></form>'
        ),
    )
    redirect = MagicMock(
        status_code=302,
        headers={"Location": "https://www.airbnb.com.sg/"},
        url="https://www.airbnb.com.sg/",
        text="",
    )
    landing_page = MagicMock(
        status_code=200,
        headers={},
        url="https://www.airbnb.com.sg/",
        text="regional shell without embedded API key",
    )
    client = MagicMock()
    client.get = AsyncMock(side_effect=[homepage, landing_page])
    client.post = AsyncMock(return_value=redirect)
    monkeypatch.setenv("AIRBNB_PUBLIC_API_KEY", "configured-public-key")
    monkeypatch.setattr(airbnb_mod, "_AIRBNB_API_KEY_CACHE", None)
    monkeypatch.setattr(airbnb_mod, "_AIRBNB_REGION_CACHE", None)

    key, base_url = await airbnb_mod._resolve_public_api_key(client)

    assert key == "configured-public-key"
    assert base_url == "https://www.airbnb.com.sg"
    client.post.assert_awaited_once()
    assert client.get.await_args_list[0].args[0] == "https://www.airbnb.com/"
    assert client.get.await_args_list[1].args[0] == "https://www.airbnb.com.sg/"
    cached_key, cached_base = await airbnb_mod._resolve_public_api_key(client)
    assert (cached_key, cached_base) == (key, base_url)
    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_airbnb_homepage_key_does_not_skip_regional_handoff(monkeypatch):
    homepage = MagicMock(
        status_code=200,
        headers={},
        url="https://www.airbnb.com/",
        text=(
            '{"api_config":{"key":"default-public-key"}}'
            '<form action="https://www.airbnb.com.sg/">'
            '<input name="payload" value="public-handoff-payload"></form>'
        ),
    )
    redirect = MagicMock(
        status_code=302,
        headers={"Location": "https://www.airbnb.com.sg/"},
        url="https://www.airbnb.com.sg/",
        text="",
    )
    regional_page = MagicMock(
        status_code=200,
        headers={},
        url="https://www.airbnb.com.sg/",
        text="regional shell without embedded API key",
    )
    client = MagicMock()
    client.get = AsyncMock(side_effect=[homepage, regional_page])
    client.post = AsyncMock(return_value=redirect)
    monkeypatch.delenv("AIRBNB_PUBLIC_API_KEY", raising=False)
    monkeypatch.setattr(airbnb_mod, "_AIRBNB_API_KEY_CACHE", None)
    monkeypatch.setattr(airbnb_mod, "_AIRBNB_REGION_CACHE", None)

    key, base_url = await airbnb_mod._resolve_public_api_key(client)

    assert key == "default-public-key"
    assert base_url == "https://www.airbnb.com.sg"
    client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_airbnb_public_api_key_rejects_offsite_handoff(monkeypatch):
    monkeypatch.delenv("AIRBNB_PUBLIC_API_KEY", raising=False)
    homepage = MagicMock(
        status_code=200,
        headers={},
        url="https://www.airbnb.com/",
        text=(
            '<form action="https://example.test/">'
            '<input name="payload" value="public-handoff-payload"></form>'
        ),
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=homepage)
    client.post = AsyncMock()
    monkeypatch.setattr(airbnb_mod, "_AIRBNB_API_KEY_CACHE", None)
    monkeypatch.setattr(airbnb_mod, "_AIRBNB_REGION_CACHE", None)

    with pytest.raises(RuntimeError, match="public_handoff_rejected"):
        await airbnb_mod._resolve_public_api_key(client)

    client.post.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_airbnb_host_profile_falls_back_for_missing_graphql_bio(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs):
            self.headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(airbnb_mod.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        airbnb_mod,
        "_resolve_public_api_key",
        AsyncMock(return_value=("public-test-key", "https://www.airbnb.com")),
    )
    collector = AirbnbCollector()
    collector._graphql = AsyncMock(return_value={
        "data": {
            "userProfile": {
                "userId": "350595032",
                "displayName": "Public Host",
                "profilePictureUrl": "https://a0.muscache.com/avatar.jpg",
                "ratingAverage": 4.9,
                "ratingCount": 20,
                "responseRate": "98%",
            },
        },
    })
    collector.probe_profile = AsyncMock(return_value=airbnb_mod.ProfileResult(
        exists=True,
        username="350595032",
        fields={
            "display_name": "Public Host",
            "avatar_url": "https://a0.muscache.com/avatar.jpg",
            "bio": "A short public host biography.",
        },
    ))

    result = await collector._fetch_host_profile(
        "350595032", "https://www.airbnb.com/users/show/350595032",
    )

    assert result["bio"] == "A short public host biography."
    assert result["rating"] == 4.9
    assert result["response_rate"] == 98.0
    collector.probe_profile.assert_awaited_once_with("350595032")


@pytest.mark.asyncio
async def test_airbnb_host_profile_skips_page_request_when_graphql_has_bio(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs):
            self.headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(airbnb_mod.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        airbnb_mod,
        "_resolve_public_api_key",
        AsyncMock(return_value=("public-test-key", "https://www.airbnb.com")),
    )
    collector = AirbnbCollector()
    collector._graphql = AsyncMock(return_value={
        "data": {
            "userProfile": {
                "userId": "350595032",
                "displayName": "Public Host",
                "hostProfileDescription": "GraphQL biography.",
            },
        },
    })
    collector.probe_profile = AsyncMock()

    result = await collector._fetch_host_profile(
        "350595032", "https://www.airbnb.com/users/show/350595032",
    )

    assert result["bio"] == "GraphQL biography."
    collector.probe_profile.assert_not_awaited()


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


def test_airbnb_pdp_parser_falls_back_to_public_localized_location():
    payload = {
        "data": {
            "node": {
                "pdpPresentation": {
                    "localizedLocation": "Public display area",
                },
            },
            "presentation": {
                "stayProductDetailPage": {
                    "sections": [
                        {"sectionId": "TITLE_DEFAULT", "section": {"title": "Harbor Loft"}},
                        {"sectionId": "LOCATION_DEFAULT", "section": {"subtitle": ""}},
                    ],
                },
            },
        },
    }

    result = airbnb_mod._parse_airbnb_listing(payload, "123456")

    assert result is not None
    assert result["location"] == "Public display area"


def test_airbnb_pdp_parser_extracts_description_photos_amenities_and_host_bio():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": [
                        {"sectionId": "TITLE_DEFAULT", "section": {"title": "Harbor Loft"}},
                        {"sectionId": "DESCRIPTION_DEFAULT", "section": {
                            "htmlDescription": {"htmlText": "<p>Quiet harbor view</p>"},
                        }},
                        {"sectionId": "MEET_YOUR_HOST", "section": {
                            "cardData": {
                                "userId": "5821", "name": "Public Host",
                                "responseRate": "98%",
                            },
                            "hostProfileDescription": {"htmlText": "<p>Local host</p>"},
                        }},
                        {"sectionId": "PHOTO_TOUR", "section": {
                            "__typename": "PhotoTourModalSection",
                            "mediaItems": [{
                                "baseUrl": "https://a0.muscache.com/photo.jpg",
                                "accessibilityLabel": "Living room",
                            }],
                        }},
                    ]
                }
            },
            "node": {"pdpPresentation": {"amenities": {"seeAllAmenitiesGroups": [
                {"title": "Bathroom", "amenities": [
                    {"title": "Hair dryer", "available": True},
                ]},
            ]}}},
        }
    }

    result = airbnb_mod._parse_airbnb_listing(payload, "123456")

    assert result is not None
    assert result["description"] == "Quiet harbor view"
    assert result["photos"] == [{
        "url": "https://a0.muscache.com/photo.jpg", "title": "Living room",
    }]
    assert result["amenities"][0]["amenities"][0]["title"] == "Hair dryer"
    assert result["host_bio"] == "Local host"
    assert result["host_response_rate"] == 98.0


def test_airbnb_parser_reads_public_modal_description_and_host_info():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {"sections": [
                        {"sectionId": "TITLE_DEFAULT", "section": {"title": "Public Stay"}},
                        {"sectionId": "DESCRIPTION_DEFAULT", "section": {
                            "__typename": "PdpDescriptionSection",
                        }},
                        {"sectionId": "DESCRIPTION_MODAL", "section": {
                            "__typename": "GeneralListContentSection",
                            "items": [
                                {"html": {"htmlText": "<p>Public listing description</p>"}},
                                {"html": {"htmlText": "<p>Additional details</p>"}},
                            ],
                        }},
                        {"sectionId": "MEET_YOUR_HOST", "section": {
                            "__typename": "MeetYourHostSection",
                        }},
                    ]},
                }
            },
            "node": {"pdpPresentation": {"hostInfo": {
                "passportData": {
                    "userId": "VXNlclByb2ZpbGU6NTgyMQ==",
                    "name": "Public Host",
                    "profilePictureUrl": "https://a0.muscache.com/host.jpg",
                    "ratingAverage": 4.9,
                    "ratingCount": 20,
                },
                "responseRateText": "Responds promptly · 98% response rate",
                "about": None,
            }}},
        }
    }

    result = airbnb_mod._parse_airbnb_listing(payload, "123456")

    assert result is not None
    assert result["description"] == "Public listing description"
    assert result["host_id"] == "5821"
    assert result["host_name"] == "Public Host"
    assert result["host_avatar_url"] == "https://a0.muscache.com/host.jpg"
    assert result["host_rating"] == 4.9
    assert result["host_reviews_count"] == 20
    assert result["host_response_rate"] == 98.0
    assert result["host_bio"] is None


def test_airbnb_parser_ignores_duplicate_layout_section_rows():
    payload = {
        "data": {"presentation": {"stayProductDetailPage": {"sections": {
            "sections": [
                {"sectionId": "DESCRIPTION_DEFAULT", "section": {
                    "__typename": "PdpDescriptionSection",
                    "htmlDescription": {"htmlText": "<p>Public room details</p>"},
                }},
                {"sectionId": "PHOTO_TOUR_SCROLLABLE_MODAL", "section": {
                    "__typename": "PhotoTourModalSection",
                    "mediaItems": [{
                        "baseUrl": "https://a0.muscache.com/photo.jpg",
                        "accessibilityLabel": "Living room",
                    }],
                }},
            ],
            "sbuiData": {"sectionConfiguration": {"root": {"sections": [
                {"sectionId": "DESCRIPTION_DEFAULT", "sectionData": {"__typename": "LayoutSection"}},
                {"sectionId": "PHOTO_TOUR_SCROLLABLE_MODAL", "sectionData": {"__typename": "LayoutSection"}},
            ]}}},
        }}}},
    }

    result = airbnb_mod._parse_airbnb_listing(payload, "123456")

    assert result is not None
    assert result["description"] == "Public room details"
    assert result["photos"] == [{
        "url": "https://a0.muscache.com/photo.jpg", "title": "Living room",
    }]


def test_airbnb_availability_parser_and_price_range_are_bounded():
    payload = {"data": {"merlin": {"pdpAvailabilityCalendar": {"calendarMonths": [
        {"year": 2026, "month": 10, "days": [
            {"date": "2026-10-14", "available": True, "price": {"amount": 180, "currency": "SGD"}},
            {"date": "2026-10-16", "available": True, "minimumNights": 2,
             "price": {"amount": 195, "currency": "SGD"}},
            {"date": "2026-10-17", "available": True},
            {"date": "2026-10-18", "available": False},
        ]},
    ]}}}}

    rows = airbnb_mod._parse_airbnb_calendar(payload)
    selected = airbnb_mod._first_available_stay(rows, date(2026, 10, 15))

    assert len(rows) == 4
    assert rows[0]["date"] == "2026-10-14"
    assert selected == (date(2026, 10, 16), date(2026, 10, 18))
    assert airbnb_mod._airbnb_money(rows[1]["price"], rows[1]["currency"]) == (195.0, "SGD")


def test_airbnb_price_parser_reads_public_structured_display_price():
    payload = {"data": {"presentation": {"stayProductDetailPage": {"sections": [
        {"sectionId": "BOOK_IT_SIDEBAR", "section": {
            "structuredDisplayPrice": {
                "primaryLine": {"discountedPrice": "S$245", "originalPrice": "S$290"},
            },
        }},
    ]}}}}

    assert airbnb_mod._parse_airbnb_price(payload) == (245.0, "SGD")


@pytest.mark.asyncio
async def test_airbnb_public_listing_page_extracts_deferred_details_payload():
    details_payload = {
        "data": {"presentation": {"stayProductDetailPage": {"sections": [
            {"sectionId": "TITLE_DEFAULT", "section": {"title": "Harbor Loft"}},
        ]}}},
    }
    deferred_state = {"niobeClientData": [["public-route", details_payload]]}
    response = MagicMock(
        status_code=200,
        headers={},
        text=(
            '<html><script id="data-deferred-state-0" type="application/json">'
            + json.dumps(deferred_state)
            + "</script></html>"
        ),
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    result = await airbnb_mod._fetch_public_listing_page(
        client, "123456", base_url="https://www.airbnb.com.sg",
    )

    assert client.get.await_args.args[0] == "https://www.airbnb.com.sg/rooms/123456"
    assert airbnb_mod._parse_airbnb_listing(result, "123456")["title"] == "Harbor Loft"


@pytest.mark.asyncio
async def test_airbnb_public_listing_page_rejects_offsite_redirect():
    response = MagicMock(status_code=302, headers={"Location": "https://evil.example/rooms/123456"})
    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    with pytest.raises(RuntimeError, match="public_redirect_rejected"):
        await airbnb_mod._fetch_public_listing_page(client, "123456")

    assert client.get.await_count == 1


def test_airbnb_detail_and_review_variables_include_date_range_and_caps():
    detail = airbnb_mod._stays_detail_variables(
        "123456", date(2026, 11, 1), date(2026, 11, 2),
    )
    review = airbnb_mod._review_variables("123456")

    assert detail["pdpSectionsRequest"]["checkIn"] == "2026-11-01"
    assert detail["pdpSectionsRequest"]["checkOut"] == "2026-11-02"
    assert review["pdpReviewsRequest"]["limit"] == 10
    assert review["pdpReviewsRequest"]["sortingPreference"] == "MOST_RECENT"


def test_airbnb_retry_after_supports_delta_and_http_date():
    now = airbnb_mod.datetime(2026, 10, 14, 17, 0, tzinfo=airbnb_mod.timezone.utc)
    assert airbnb_mod._retry_after_seconds("75", now=now) == 75
    assert airbnb_mod._retry_after_seconds("Wed, 14 Oct 2026 17:02:00 GMT", now=now) == 120
    assert airbnb_mod._retry_after_seconds("invalid", now=now) is None


@pytest.mark.asyncio
async def test_airbnb_graphql_uses_operation_route_slash_when_required():
    response = MagicMock(status_code=200, headers={}, json=lambda: {"data": {}})
    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    await AirbnbCollector._graphql(
        client, "PdpAvailabilityCalendar", "calendar-hash", {},
        base_url="https://www.airbnb.com.sg", trailing_slash=True,
    )
    assert client.get.await_args.args[0] == (
        "https://www.airbnb.com.sg/api/v3/PdpAvailabilityCalendar/calendar-hash/"
    )

    await AirbnbCollector._graphql(client, "StaysPdpSections", "price-hash", {})
    assert client.get.await_args.args[0].endswith("/StaysPdpSections/price-hash")


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
            if (
                query.lstrip().startswith("INSERT INTO browser_native_scrape_queue")
                and "'airbnb_listings'" in query
            ):
                self.listing_limit = args[0]
                self.seeded_count = min(5, args[0])

        async def fetchval(self, query, *args):
            if (
                "WHERE platform='airbnb'" in query
                and "status IN ('pending','claimed')" in query
            ):
                assert "status IN ('pending','claimed')" in query
                return active_count
            assert args == ("airbnb", "followed_account_link")
            return 0

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


@pytest.mark.asyncio
async def test_airbnb_followed_profile_links_are_seeded_before_broad_discovery(monkeypatch):
    from src.collectors import native_platform_scrape as native_scrape

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Connection:
        def __init__(self):
            self.executed = []

        def transaction(self):
            return Transaction()

        async def execute(self, query, *args):
            self.executed.append((query, args))

        async def fetchval(self, _query, *_args):
            return 0

    class Pool:
        def __init__(self):
            self.conn = Connection()

        @asynccontextmanager
        async def acquire(self):
            yield self.conn

    monkeypatch.setattr(native_scrape, "_SEED_QUERIES", {"airbnb": []})
    pool = Pool()

    await seed_native_targets(pool, "airbnb")

    query, args = next(
        (query, args) for query, args in pool.conn.executed
        if query == _FOLLOWED_AIRBNB_PROFILE_URLS_SQL
    )
    assert args[:2] == (native_scrape._FOLLOWED_TARGET_PRIORITY, native_scrape._FOLLOWED_TARGET_CAP)
    assert "youtube_channels c" in query
    assert "facebook_profiles p" in query
    assert "bluesky_profiles p" in query
    assert "pinterest_profiles p" in query
    assert "snapchat_profiles p" in query
    assert "social_users s" in query and "f.platform='threads'" in query
    assert "c.last_skip_reason IS DISTINCT FROM 'verified_account'" in query
    assert "strava_athletes" not in query
    assert "facebook_posts p" in native_scrape._FOLLOWED_AIRBNB_LINKS_SQL
    assert re.search(args[2], "bio: https://www.airbnb.com/rooms/12345").group(1) == "12345"
    assert re.search(args[2], "https://notairbnb.com/rooms/12345") is None


@pytest.mark.asyncio
async def test_airbnb_empty_calendar_preserves_stored_availability_and_check_time(monkeypatch):
    from src.collectors import native_platform_scrape as native_scrape

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Connection:
        def __init__(self):
            self.executed = []

        def transaction(self):
            return Transaction()

        async def execute(self, query, *args):
            self.executed.append((query, args))

    class Pool:
        def __init__(self):
            self.conn = Connection()

        @asynccontextmanager
        async def acquire(self):
            yield self.conn

    pool = Pool()
    monkeypatch.setenv("AIRBNB_NATIVE_ENABLED", "1")
    monkeypatch.setattr(native_scrape, "seed_native_targets", AsyncMock())
    monkeypatch.setattr(
        native_scrape,
        "claim_native_target",
        AsyncMock(return_value={
            "id": 7,
            "target_url": "https://www.airbnb.com/rooms/123456",
            "attempts": 1,
            "claim_token": "00000000-0000-0000-0000-000000000001",
        }),
    )
    monkeypatch.setattr(native_scrape, "finish_native_target", AsyncMock())

    collector = AirbnbCollector()
    collector.pool = pool
    collector._fetch_listing_data = AsyncMock(return_value=(
        {
            "listing_id": "123456", "host_id": None, "host_name": None,
            "host_profile_url": None, "title": "Public Stay",
            "url": "https://www.airbnb.com/rooms/123456", "location": None,
            "image_url": None, "rating": None, "reviews_count": 0,
            "property_type": None, "bedrooms": None, "bathrooms": None,
            "max_guests": None, "description": None, "photos": [],
            "price_amount": None, "price_currency": None,
            "price_check_in": None, "price_check_out": None, "amenities": [],
        },
        [],
        [],
    ))

    summary = await collector.run_native_cycle()

    listing_query = next(
        query for query, _args in pool.conn.executed
        if "INSERT INTO airbnb_listings" in query
    )
    assert "availability=CASE WHEN jsonb_array_length(EXCLUDED.availability)>0" in listing_query
    assert "THEN EXCLUDED.availability ELSE airbnb_listings.availability END" in listing_query
    assert "CASE WHEN jsonb_array_length($22::jsonb)>0 THEN now() ELSE NULL END" in listing_query
    assert "WHEN jsonb_array_length(EXCLUDED.availability)>0 THEN now()" in listing_query
    assert "ELSE airbnb_listings.availability_checked_at END" in listing_query
    assert summary["availability_days"] == 0
