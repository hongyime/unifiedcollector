from types import SimpleNamespace

import pytest

from src.bridges.ig_ingest.native_scrape import native_scrape_target
from src.bridges.ig_ingest.native_scrape import (
    _platform_url_allowed,
    _same_target,
    _validated_item,
)


def test_native_page_targets_allow_only_expected_platform_hosts():
    assert _platform_url_allowed("pinterest", "https://www.pinterest.com/alice/")
    assert _platform_url_allowed("snapchat", "https://www.snapchat.com/add/alice")
    assert _platform_url_allowed("airbnb", "https://www.airbnb.com.sg/rooms/123")
    assert not _platform_url_allowed("airbnb", "http://www.airbnb.com/rooms/123")
    assert not _platform_url_allowed("snapchat", "https://example.com/add/alice")


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["pinterest", "snapchat", "airbnb"])
async def test_native_target_endpoint_leaves_claims_to_python_collectors(platform, monkeypatch):
    import src.bridges.ig_ingest as bridge

    async def unexpected_pool_access():
        raise AssertionError("browser endpoint must not claim native collector work")

    monkeypatch.setattr(bridge, "get_pool", unexpected_pool_access)
    response = await native_scrape_target(SimpleNamespace(query={"platform": platform}))

    assert response.status == 200
    assert response.text == '{"ok": true, "target": null}'


def test_native_target_matches_allow_airbnb_locale_redirects():
    assert _same_target(
        "airbnb",
        "https://www.airbnb.com.sg/rooms/123?source=search",
        "https://www.airbnb.com/rooms/123",
    )
    assert not _same_target(
        "airbnb",
        "https://www.airbnb.com.sg/rooms/124",
        "https://www.airbnb.com/rooms/123",
    )
    assert _same_target(
        "snapchat",
        "https://www.snapchat.com/add/alice/",
        "https://snapchat.com/add/alice",
    )
    assert _same_target(
        "snapchat",
        "https://www.snapchat.com/@alice",
        "https://www.snapchat.com/add/alice/",
    )
    assert _same_target(
        "snapchat",
        "https://www.snapchat.com/discover?web_client_id=public",
        "https://www.snapchat.com/discover",
    )
    assert _same_target(
        "pinterest",
        "https://www.pinterest.com/alice/_created/",
        "https://www.pinterest.com/alice/",
    )


def test_native_item_validation_builds_platform_rows():
    pin = _validated_item("pinterest", {
        "type": "pin", "id": "123", "url": "https://www.pinterest.com/pin/123/",
        "username": "alice", "board_id": "alice:travel",
        "board_url": "https://www.pinterest.com/alice/travel/",
    })
    story = _validated_item("snapchat", {
        "type": "story", "id": "story_1", "url": "https://www.snapchat.com/add/alice",
        "username": "alice", "media_url": "https://cf-st.sc-cdn.net/story.mp4",
    })
    listing = _validated_item("airbnb", {
        "type": "listing", "id": "123", "url": "https://www.airbnb.com/rooms/123",
        "host_profile_url": "https://www.airbnb.com/users/show/55",
        "rating": 4.98, "bedrooms": 12, "bathrooms": 1.5, "max_guests": 18,
    })
    assert pin is not None and pin[0] == "pin"
    assert story is not None and story[0] == "story"
    assert listing is not None and listing[0] == "listing"
    assert listing[1][11] == 12.0


@pytest.mark.parametrize("content_type", ["spotlight", "highlight"])
def test_native_snapchat_item_routes_content_types_to_separate_tables(content_type):
    item = _validated_item("snapchat", {
        "type": "story", "id": "snap_1", "url": "https://www.snapchat.com/spotlight",
        "metadata": {"content_type": content_type},
    })

    assert item is not None
    assert item[0] == content_type


def test_native_item_validation_rejects_untrusted_urls_and_ids():
    assert _validated_item("pinterest", {
        "type": "pin", "id": "bad/id", "url": "https://www.pinterest.com/pin/123/",
    }) is None
    assert _validated_item("snapchat", {
        "type": "story", "id": "story_1", "url": "https://example.com/story/1",
    }) is None
    assert _validated_item("airbnb", {
        "type": "listing", "id": "123", "url": "https://evil.example/rooms/123",
    }) is None
