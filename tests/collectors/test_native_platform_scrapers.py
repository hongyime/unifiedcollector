from __future__ import annotations

import json

import pytest

from src.collectors.pinterest import (
    PinterestCollector,
    _contains_private_marker,
    _fetch_pinterest_html,
    _load_pinterest_storage_state,
    _pinterest_page_kind,
    _verified_board_link_conflict,
)
from src.collectors import pinterest as pinterest_mod
from src.collectors import snapchat as snapchat_mod
from src.collectors.snapchat import _allowed_public_page, _extract_public_snap_items


def test_pinterest_cookie_snapshot_filters_to_pinterest_domain(tmp_path):
    snapshot = tmp_path / "cookies.json"
    snapshot.write_text(json.dumps({"cookies": [
        {"name": "session", "value": "cookie-value", "domain": ".pinterest.com", "path": "/", "secure": True},
        {"name": "other", "value": "ignored", "domain": ".example.com", "path": "/"},
    ]}), encoding="utf-8")

    result = _load_pinterest_storage_state(snapshot)

    assert result is not None
    assert [cookie["name"] for cookie in result["cookies"]] == ["session"]


def test_pinterest_public_page_parser_extracts_board_and_pin_cards():
    boards, pins = PinterestCollector._parse_native_page(
        "https://www.pinterest.com/alice/travel/",
        {
            "title": "Travel",
            "primary_heading": "Travel",
            "board_header_text": "Travel board 42 pins",
            "body_text": "Travel board 42 pins",
            "anchors": [
                {
                    "href": "https://www.pinterest.com/pin/12345/",
                    "text": "Mountain route",
                    "image_alt": "Mountain route photo",
                    "image_url": "https://i.pinimg.com/image.jpg",
                },
                {
                    "href": "https://www.pinterest.com/alice/travel/",
                    "pin_id": "67890",
                    "text": "Coast route",
                    "image_url": "https://i.pinimg.com/coast.jpg",
                },
            ],
        },
    )

    assert boards[0]["pin_count"] == 42
    assert {pin["id"] for pin in pins} == {"12345", "67890"}
    assert all(pin["username"] is None for pin in pins)
    assert all(pin["owner_verified"] is False for pin in pins)


def test_pinterest_pin_ownership_requires_exact_visible_membership():
    _boards, pins = PinterestCollector._parse_native_page(
        "https://www.pinterest.com/alice/travel/",
        {
            "title": "Travel",
            "primary_heading": "Travel",
            "board_header_text": "Travel board 42 pins",
            "body_text": "Travel board 42 pins",
            "anchors": [
                {
                    "href": "https://www.pinterest.com/pin/12345/",
                    "membership_links": [
                        "https://www.pinterest.com/alice/travel/",
                    ],
                },
                {
                    "href": "https://www.pinterest.com/pin/67890/",
                    "membership_links": [
                        "https://www.pinterest.com/alice/recipes/",
                    ],
                },
                {
                    "href": "https://www.pinterest.com/pin/11111/",
                    "membership_links": [
                        "https://www.pinterest.com/other/travel/",
                    ],
                },
            ],
        },
    )

    by_id = {pin["id"]: pin for pin in pins}
    assert by_id["12345"]["owner_verified"] is True
    assert by_id["12345"]["username"] == "alice"
    assert by_id["12345"]["board_id"] == "alice/travel"
    assert by_id["67890"]["owner_verified"] is False
    assert by_id["67890"]["username"] is None
    assert by_id["11111"]["owner_verified"] is False


def test_pinterest_profile_pin_ownership_needs_verified_identity_and_matching_link():
    target = "https://www.pinterest.com/alice/"
    anchors = [
        {
            "href": "https://www.pinterest.com/pin/12345/",
            "membership_links": ["https://www.pinterest.com/alice/travel/"],
        },
        {
            "href": "https://www.pinterest.com/pin/67890/",
            "membership_links": ["https://www.pinterest.com/other/travel/"],
        },
    ]

    _boards, unverified_pins = PinterestCollector._parse_native_page(
        target, {"anchors": anchors, "profile_identity_verified": False},
    )
    _boards, verified_pins = PinterestCollector._parse_native_page(
        target, {"anchors": anchors, "profile_identity_verified": True},
    )

    assert all(pin["owner_verified"] is False for pin in unverified_pins)
    assert {pin["id"] for pin in verified_pins if pin["owner_verified"]} == {"12345"}


def test_pinterest_upsert_preserves_verified_board_link_without_new_board_context():
    expression = " ".join(_verified_board_link_conflict("board_id").split())

    assert "EXCLUDED.metadata->>'owner_verified'='true' AND EXCLUDED.board_id IS NOT NULL" in expression
    assert "THEN EXCLUDED.board_id" in expression
    assert "pinterest_pins.username IS NOT DISTINCT FROM EXCLUDED.username" in expression
    assert "THEN pinterest_pins.board_id" in expression
    assert "WHEN EXCLUDED.metadata->>'owner_verified'='true' THEN NULL" in expression
    assert expression.endswith("ELSE NULL END")

    with pytest.raises(ValueError, match="unsupported_board_link_column"):
        _verified_board_link_conflict("username")


def test_pinterest_target_validation_rejects_nonpublic_routes():
    assert _pinterest_page_kind("https://www.pinterest.com/alice/travel/") == ("board", "alice", "travel")
    assert _pinterest_page_kind("https://www.pinterest.com/alice/_saved/") is None
    assert _pinterest_page_kind("http://www.pinterest.com/alice/") is None
    assert _pinterest_page_kind("https://evil.example/alice/") is None


def test_pinterest_authenticated_content_is_limited_to_anonymous_public_ids():
    public_boards = [{"id": "alice/travel", "name": "Travel", "pin_count": 42}]
    public_pins = [{"id": "123"}]
    authenticated_boards = [
        {"id": "alice/travel", "name": "Authenticated title", "pin_count": 99},
        {"id": "alice/secret", "name": "Secret", "pin_count": 1},
    ]
    authenticated_pins = [{"id": "123"}, {"id": "999"}]

    boards, pins = PinterestCollector._intersect_public_content(
        public_boards, public_pins, authenticated_boards, authenticated_pins,
    )

    assert boards == public_boards
    assert pins == public_pins


def test_pinterest_board_is_not_public_without_its_anonymous_heading_and_count():
    boards, pins = PinterestCollector._parse_native_page(
        "https://www.pinterest.com/alice/travel/",
        {
            "title": "Travel",
            "body_text": "Some unrelated panel 12 pins",
            "anchors": [{"href": "https://www.pinterest.com/pin/123/"}],
        },
    )

    assert pins
    assert not PinterestCollector._has_public_content(
        ("board", "alice", "travel"), boards, pins,
    )


def test_pinterest_private_markers_are_detected_in_page_and_serialized_state():
    assert _contains_private_marker({"body_text": "This is a secret board"})
    assert _contains_private_marker({"serialized_state": '{"is_secret": true}'})
    assert not _contains_private_marker({"body_text": "Public travel board"})


@pytest.mark.asyncio
async def test_pinterest_rejects_offsite_redirect_before_following_it(monkeypatch):
    class Response:
        status_code = 302
        headers = {"location": "https://evil.example/alice/"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url, **_kwargs):
            self.calls.append((method, url))
            return Response()

    client = Client(follow_redirects=False)
    monkeypatch.setattr(pinterest_mod.httpx, "AsyncClient", lambda **_kwargs: client)

    with pytest.raises(RuntimeError, match="public_redirect_rejected"):
        await _fetch_pinterest_html(
            "https://www.pinterest.com/alice/",
            ("profile", "alice", None),
            storage_state=None,
        )

    assert len(client.calls) == 1


def test_snapchat_allowlist_limits_requests_to_public_pages():
    assert _allowed_public_page("https://www.snapchat.com/discover")
    assert _allowed_public_page("https://www.snapchat.com/spotlight")
    assert _allowed_public_page("https://www.snapchat.com/@alice")
    assert not _allowed_public_page("https://www.snapchat.com/chat/alice")
    assert not _allowed_public_page("http://www.snapchat.com/discover")
    assert not _allowed_public_page("https://evil.example/discover")


def test_snapchat_rejects_unsafe_redirect_before_second_request(monkeypatch):
    class Response:
        status_code = 302
        headers = {"Location": "https://evil.example/collect"}
        url = "https://www.snapchat.com/discover"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Session:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    session = Session()
    monkeypatch.setattr(snapchat_mod.requests, "Session", lambda: session)

    try:
        snapchat_mod.SnapchatCollector._fetch_public_page("https://www.snapchat.com/discover")
    except RuntimeError as exc:
        assert str(exc) == "public_redirect_rejected"
    else:
        raise AssertionError("unsafe redirect was followed")

    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False


def test_snapchat_public_parser_reads_highlight_and_resolves_public_profile():
    html = '''<script id="__NEXT_DATA__" type="application/json">{
      "props":{"pageProps":{"initialApollo":{"state":{
        "story-1":{"__typename":"SnapProStory","storyId":"story-1","type":"highlight",
          "creator":{"__ref":"User:1"},"snaps":[{"__ref":"Snap:1"}]},
        "User:1":{"__typename":"PublicProfile","username":"alice"},
        "Snap:1":{"__typename":"Snap","contentUrl":"https://cf-st.sc-cdn.net/story.mp4"}
      }}}}}
    </script>'''

    items = _extract_public_snap_items(html, "https://www.snapchat.com/@alice", "alice")

    assert len(items) == 1
    assert items[0]["username"] == "alice"
    assert items[0]["media_url"] == "https://cf-st.sc-cdn.net/story.mp4"
    assert items[0]["metadata"]["content_type"] == "highlight"


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("public_story", ("snapchat_stories", "story_id")),
        ("spotlight", ("snapchat_spotlights", "spotlight_id")),
        ("highlight", ("snapchat_highlights", "highlight_id")),
    ],
)
def test_snapchat_public_content_uses_its_own_table(content_type, expected):
    assert snapchat_mod._snapchat_storage_target(content_type) == expected


def test_snapchat_storage_rejects_unrecognized_content_types():
    with pytest.raises(ValueError, match="unsupported_snapchat_content_type"):
        snapchat_mod._snapchat_storage_target("private_story")
