from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.collectors.pinterest import (
    PinterestCollector,
    _PinterestPublicHttpError,
    _contains_private_marker,
    _fetch_pinterest_html,
    _load_pinterest_storage_state,
    _pinterest_page_kind,
    _retry_after_seconds as _pinterest_retry_after_seconds,
    _same_pinterest_page_kind,
    _verified_board_link_conflict,
)
from src.collectors import pinterest as pinterest_mod
from src.collectors import snapchat as snapchat_mod
from src.collectors.native_platform_scrape import _SEED_QUERIES, retarget_snapchat_profile_targets
from src.collectors.snapchat import (
    _PublicPageHttpError,
    _allowed_public_page,
    _extract_public_snap_items,
    _retry_after_seconds,
    _snapchat_add_profile_url,
)


def test_pinterest_cookie_snapshot_filters_to_pinterest_domain(tmp_path):
    snapshot = tmp_path / "cookies.json"
    snapshot.write_text(json.dumps({"cookies": [
        {"name": "session", "value": "cookie-value", "domain": ".pinterest.com", "path": "/", "secure": True},
        {"name": "other", "value": "ignored", "domain": ".example.com", "path": "/"},
    ]}), encoding="utf-8")

    result = _load_pinterest_storage_state(snapshot)

    assert result is not None
    assert [cookie["name"] for cookie in result["cookies"]] == ["session"]


def test_pinterest_cookie_loader_uses_newest_platform_snapshot(tmp_path, monkeypatch):
    (tmp_path / "latest.json").write_text(json.dumps({"cookies": [
        {"name": "session", "value": "effective-old", "domain": ".pinterest.com", "path": "/"},
    ]}), encoding="utf-8")
    (tmp_path / "snapshot_20261015T100000Z.json").write_text(json.dumps({"cookies": [
        {"name": "session", "value": "newer-pinterest", "domain": ".pinterest.com", "path": "/"},
    ]}), encoding="utf-8")
    (tmp_path / "snapshot_20261015T100500Z.json").write_text(json.dumps({"cookies": [
        {"name": "session", "value": "lemon8-only", "domain": ".lemon8-app.com", "path": "/"},
    ]}), encoding="utf-8")
    monkeypatch.setenv("BROWSER_COOKIE_VAULT_DIR", str(tmp_path))

    result = _load_pinterest_storage_state()

    assert result is not None
    assert [cookie["value"] for cookie in result["cookies"]] == ["newer-pinterest"]


def test_pinterest_public_page_parser_extracts_board_and_pin_cards():
    boards, pins = PinterestCollector._parse_native_page(
        "https://www.pinterest.com/alice/travel/",
        {
            "title": "Travel",
            "primary_heading": "Travel",
            "board_header_text": "Travel board 42 pins · 3 followers",
            "description": "Trips and routes",
            "body_text": "Travel board 42 pins",
            "anchors": [
                {
                    "href": "https://www.pinterest.com/pin/12345/",
                    "text": "Mountain route",
                    "image_alt": "Mountain route photo",
                    "image_url": "https://i.pinimg.com/image.jpg",
                    "card_text": "2 repins 4 comments",
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
    assert boards[0]["description"] == "Trips and routes"
    assert boards[0]["follower_count"] == 3
    assert {pin["id"] for pin in pins} == {"12345", "67890"}
    assert next(pin for pin in pins if pin["id"] == "12345")["repin_count"] == 2
    assert next(pin for pin in pins if pin["id"] == "12345")["comment_count"] == 4
    assert all(pin["username"] is None for pin in pins)
    assert all(pin["owner_verified"] is False for pin in pins)


def test_pinterest_parser_extracts_pin_cards_without_anchor_links():
    _boards, pins = PinterestCollector._parse_native_page(
        "https://www.pinterest.com/alice/travel/",
        {
            "primary_heading": "Travel",
            "board_header_text": "Travel board 42 pins",
            "body_text": "Travel board 42 pins",
            "pin_cards": [
                {
                    "href": "https://www.pinterest.com/pin/123456789/",
                    "pin_id": "123456789",
                    "membership_links": ["https://www.pinterest.com/alice/travel/"],
                    "image_alt": "Mountain route photo",
                    "image_url": "https://i.pinimg.com/image.jpg",
                },
            ],
        },
    )

    assert len(pins) == 1
    assert pins[0]["id"] == "123456789"
    assert pins[0]["owner_verified"] is True
    assert pins[0]["board_id"] == "alice/travel"


def test_pinterest_parser_skips_explicit_verified_pin_authors_but_keeps_unknown_authors():
    _boards, pins = PinterestCollector._parse_native_page(
        "https://www.pinterest.com/alice/travel/",
        {
            "primary_heading": "Travel",
            "board_header_text": "Travel board 42 pins",
            "pin_cards": [
                {
                    "href": "https://www.pinterest.com/pin/111/",
                    "pin_id": "111",
                    "author_is_verified": True,
                },
                {
                    "href": "https://www.pinterest.com/pin/222/",
                    "pin_id": "222",
                    "author_is_verified": "yes",
                },
                {
                    "href": "https://www.pinterest.com/pin/333/",
                    "pin_id": "333",
                },
                {
                    "href": "https://www.pinterest.com/pin/444/",
                    "pin_id": "444",
                    "author_is_verified": False,
                },
            ],
        },
    )

    assert {pin["id"] for pin in pins} == {"333", "444"}


@pytest.mark.asyncio
async def test_pinterest_profile_probe_extracts_following_and_monthly_views(monkeypatch):
    response = type("Response", (), {
        "status_code": 200,
        "url": "https://www.pinterest.com/alice/",
        "text": (
            '{"full_name":"Alice","follower_count":10,"pin_count":20,"board_count":3}'
            "<main>1.2M monthly views · 35 following</main>"
        ),
    })()
    class Client:
        async def get(self, *_args, **_kwargs):
            return response

    client = Client()

    class AsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return client

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(pinterest_mod.httpx, "AsyncClient", AsyncClient)
    result = await PinterestCollector().probe_profile("alice")

    assert result.exists is True
    assert result.fields["following_count"] == 35
    assert result.fields["monthly_views"] == 1_200_000


@pytest.mark.asyncio
async def test_pinterest_profile_probe_parses_compact_serialized_counts(monkeypatch):
    response = type("Response", (), {
        "status_code": 200,
        "url": "https://www.pinterest.com/alice/",
        "text": (
            '{"full_name":"Alice","following_count":"1.4K",'
            '"monthly_views":"2.3B"}'
        ),
    })()
    class Client:
        async def get(self, *_args, **_kwargs):
            return response

    class AsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return Client()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(pinterest_mod.httpx, "AsyncClient", AsyncClient)
    result = await PinterestCollector().probe_profile("alice")

    assert result.fields["following_count"] == 1_400
    assert result.fields["monthly_views"] == 2_300_000_000


@pytest.mark.asyncio
async def test_pinterest_profile_probe_rejects_homepage_redirect(monkeypatch):
    response = type("Response", (), {
        "status_code": 200,
        "url": "https://www.pinterest.com/",
        "text": '{"full_name":"Pinterest Home","follower_count":999}',
    })()

    class Client:
        async def get(self, *_args, **_kwargs):
            return response

    class AsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return Client()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(pinterest_mod.httpx, "AsyncClient", AsyncClient)
    result = await PinterestCollector().probe_profile("alice")

    assert result.exists is False
    assert result.fields == {}
    assert result.error == "redirect:unexpected_target"


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


def test_pinterest_profile_page_identity_attributes_its_visible_pin_cards():
    target = "https://www.pinterest.com/alice/"
    _boards, pins = PinterestCollector._parse_native_page(
        target,
        {
            "url": target,
            "pin_cards": [{
                "href": "https://www.pinterest.com/pin/12345/",
                "pin_id": "12345",
                "membership_links": [],
            }],
        },
    )

    assert len(pins) == 1
    assert pins[0]["owner_verified"] is True
    assert pins[0]["username"] == "alice"


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


def test_pinterest_profile_redirect_stays_with_same_owner():
    target = ("profile", "alice", None)

    assert _same_pinterest_page_kind(
        "https://www.pinterest.com/alice/_created/", target,
    )
    assert _same_pinterest_page_kind(
        "https://www.pinterest.com/alice/travel/", target,
    )
    assert _same_pinterest_page_kind(
        "https://www.pinterest.com/ALICE/Travel/", target,
    )
    assert not _same_pinterest_page_kind(
        "https://www.pinterest.com/other/_created/", target,
    )


@pytest.mark.asyncio
async def test_pinterest_followed_handle_navigates_with_cookie_context(monkeypatch):
    import sys
    from types import ModuleType

    storage_state = {"cookies": [{"name": "session", "value": "test-cookie"}]}
    context_states = []
    read_calls = []

    class Context:
        async def close(self):
            return None

    class Browser:
        async def new_context(self, **kwargs):
            context_states.append(kwargs.get("storage_state"))
            return Context()

        async def close(self):
            return None

    class Chromium:
        async def launch(self, **_kwargs):
            return Browser()

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            return None

    class PlaywrightManager:
        async def start(self):
            return Playwright()

    playwright_module = ModuleType("playwright")
    async_api_module = ModuleType("playwright.async_api")
    async_api_module.async_playwright = lambda: PlaywrightManager()
    monkeypatch.setitem(sys.modules, "playwright", playwright_module)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api_module)
    monkeypatch.setattr(pinterest_mod, "_load_pinterest_storage_state", lambda: storage_state)

    async def fetch(*_args, **_kwargs):
        raise AssertionError("authenticated followed targets must navigate in Playwright")

    monkeypatch.setattr(pinterest_mod, "_fetch_pinterest_html", fetch)
    collector = object.__new__(PinterestCollector)

    async def read(_context, url, _target_kind, html, **kwargs):
        read_calls.append((url, html, kwargs))
        return {
            "url": url,
            "profile_identity_verified": True,
            "profile_is_verified": False,
            "pin_cards": [{
                "href": "https://www.pinterest.com/pin/12345/",
                "pin_id": "12345",
                "membership_links": [],
            }],
        }

    monkeypatch.setattr(collector, "_read_public_page", read)
    _boards, pins = await collector._scrape_public_page(
        "https://www.pinterest.com/alice/",
        target_source="followed_handle_candidate",
    )

    assert context_states == [storage_state]
    assert read_calls[0][0] == "https://www.pinterest.com/alice/"
    assert read_calls[0][1] is None
    assert read_calls[0][2]["direct_navigation"] is True
    assert pins[0]["owner_verified"] is True


@pytest.mark.asyncio
async def test_pinterest_authenticated_navigation_keeps_redirects_within_same_owner():
    class Response:
        status = 200

    class Request:
        def __init__(self, page, url):
            self.frame = page.main_frame
            self.url = url

        @staticmethod
        def is_navigation_request():
            return True

    class Route:
        def __init__(self, request):
            self.request = request
            self.action = None

        async def continue_(self):
            self.action = "continue"

        async def abort(self):
            self.action = "abort"

        async def fulfill(self, **_kwargs):
            self.action = "fulfill"

    class Page:
        def __init__(self, urls):
            self.urls = urls
            self.main_frame = object()
            self.actions = []

        async def route(self, _pattern, handler):
            self.handler = handler

        async def goto(self, _url, **_kwargs):
            for url in self.urls:
                route = Route(Request(self, url))
                await self.handler(route)
                self.actions.append(route.action)
                if route.action == "abort":
                    raise RuntimeError("navigation aborted")
            return Response()

        async def wait_for_timeout(self, _milliseconds):
            return None

        async def evaluate(self, _script, _argument=None):
            if _argument is None:
                return None
            return {
                "url": self.urls[-1],
                "profile_identity_verified": True,
                "profile_is_verified": False,
                "body_text": "",
            }

    class Context:
        def __init__(self, page):
            self.page = page

        async def new_page(self):
            return self.page

    collector = object.__new__(PinterestCollector)
    target = "https://www.pinterest.com/alice/"
    same_owner_page = Page([
        target,
        "https://www.pinterest.com/alice/_created/",
    ])
    await collector._read_public_page(
        Context(same_owner_page), target, ("profile", "alice", None), None,
        direct_navigation=True,
    )
    assert same_owner_page.actions == ["continue", "continue"]

    cross_owner_page = Page([
        target,
        "https://www.pinterest.com/other/",
    ])
    with pytest.raises(RuntimeError, match="public_redirect_rejected"):
        await collector._read_public_page(
            Context(cross_owner_page), target, ("profile", "alice", None), None,
            direct_navigation=True,
        )
    assert cross_owner_page.actions == ["continue", "abort"]


def test_pinterest_retry_after_supports_delta_and_http_date():
    now = datetime(2026, 10, 14, 17, 0, tzinfo=timezone.utc)
    assert _pinterest_retry_after_seconds("75", now=now) == 75
    assert _pinterest_retry_after_seconds("Wed, 14 Oct 2026 17:02:00 GMT", now=now) == 120
    assert _pinterest_retry_after_seconds("invalid", now=now) is None


@pytest.mark.asyncio
async def test_pinterest_http_error_preserves_retry_after_without_response_body(monkeypatch):
    class Response:
        status_code = 429
        headers = {"Retry-After": "900"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(pinterest_mod.httpx, "AsyncClient", Client)

    with pytest.raises(_PinterestPublicHttpError) as exc_info:
        await _fetch_pinterest_html(
            "https://www.pinterest.com/alice/",
            ("profile", "alice", None),
            storage_state=None,
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds == 900


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
    assert _allowed_public_page("https://www.snapchat.com/add/alice")
    assert _allowed_public_page("https://story.snapchat.com/@alice")
    assert not _allowed_public_page("https://www.snapchat.com/chat/alice")
    assert not _allowed_public_page("http://www.snapchat.com/discover")
    assert not _allowed_public_page("https://evil.example/discover")


def test_snapchat_add_profile_url_only_accepts_safe_handles():
    assert _snapchat_add_profile_url("alice") == "https://www.snapchat.com/add/alice"
    assert _snapchat_add_profile_url("@alice.name") == "https://www.snapchat.com/add/alice.name"
    assert _snapchat_add_profile_url("../outside") is None
    assert _snapchat_add_profile_url(None) is None


def test_snapchat_native_seeds_global_public_discover_and_spotlight_pages():
    queries = "\n".join(_SEED_QUERIES["snapchat"])

    assert "https://www.snapchat.com/discover" in queries
    assert "https://www.snapchat.com/spotlight" in queries
    assert "'snapchat_discover', 15" in queries
    assert "'snapchat_spotlight', 15" in queries


def test_snapchat_retry_after_supports_delta_and_http_date():
    now = datetime(2026, 10, 14, 17, 0, tzinfo=timezone.utc)
    assert _retry_after_seconds("75", now=now) == 75
    assert _retry_after_seconds("Wed, 14 Oct 2026 17:02:00 GMT", now=now) == 120
    assert _retry_after_seconds("invalid", now=now) is None
    assert _retry_after_seconds("inf", now=now) is None


@pytest.mark.asyncio
async def test_snapchat_queue_retarget_preserves_existing_schedule():
    class Conn:
        query = ""

        async def execute(self, query, *_args):
            self.query = query

    conn = Conn()
    await retarget_snapchat_profile_targets(conn)

    assert "https://story.snapchat.com/@" in conn.query
    assert "q.status <> 'claimed' OR q.claim_expires_at <= now()" in conn.query
    assert "q.claim_expires_at <= now()) THEN 0" in conn.query
    assert "next_visit_at" not in conn.query


def test_snapchat_public_http_error_keeps_only_status_and_retry_hint():
    error = _PublicPageHttpError(429, 900)
    assert str(error) == "public_http_429"
    assert error.status_code == 429
    assert error.retry_after_seconds == 900


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


def test_snapchat_story_page_parser_emits_each_public_content_type():
    html = '''<script id="__NEXT_DATA__" type="application/json">{
      "props":{"pageProps":{
        "userProfile":{"publicProfileInfo":{"username":"alice"}},
        "story":{"storyId":{"value":"active-story"},"snapList":[
          {"snapId":{"value":"story-snap"},"timestampInSec":{"value":1791997200},
           "snapMediaType":{"value":1},"snapUrls":{"mediaUrl":"https://cf-st.sc-cdn.net/story.mp4"}}
        ]},
        "curatedHighlights":[{"highlightId":{"value":"highlight-1"},
          "storyTitle":{"value":"Trip"},"snapList":[
            {"snapId":{"value":"highlight-snap"},"timestampInSec":{"value":1791997200},
             "snapUrls":{"mediaUrl":"https://cf-st.sc-cdn.net/highlight.mp4"}}
          ]}],
        "spotlightHighlights":[{"spotlightId":{"value":"spotlight-1"},
          "snapList":[{"snapId":{"value":"spotlight-snap"},
            "snapUrls":{"mediaUrl":"https://cf-st.sc-cdn.net/spotlight.mp4"}}]}
        ]
      }}
    }</script>'''

    items = _extract_public_snap_items(
        html, "https://story.snapchat.com/@alice", "alice",
    )

    assert len(items) == 3
    assert {item["metadata"]["content_type"] for item in items} == {
        "public_story", "highlight", "spotlight",
    }
    assert {item["username"] for item in items} == {"alice"}
    assert {item["media_url"] for item in items} == {
        "https://cf-st.sc-cdn.net/story.mp4",
        "https://cf-st.sc-cdn.net/highlight.mp4",
        "https://cf-st.sc-cdn.net/spotlight.mp4",
    }


@pytest.mark.asyncio
async def test_snapchat_native_scrape_falls_back_to_public_add_page_on_story_404(monkeypatch):
    profile_html = '''<script id="__NEXT_DATA__" type="application/json">{
      "props":{"pageProps":{"userProfile":{"publicProfileInfo":{"username":"alice"}},
        "curatedHighlights":[{"highlightId":"highlight-1","storyTitle":"Trip",
          "snapList":[{"snapId":"highlight-snap",
            "snapUrls":{"mediaUrl":"https://cf-st.sc-cdn.net/highlight.mp4"}}]}]}}
    }</script>'''
    calls = []

    def fetch(url):
        calls.append(url)
        if url == "https://story.snapchat.com/@alice":
            raise _PublicPageHttpError(404)
        return profile_html

    monkeypatch.setattr(
        snapchat_mod.SnapchatCollector,
        "_fetch_public_page",
        staticmethod(fetch),
    )
    collector = object.__new__(snapchat_mod.SnapchatCollector)

    final_url, items = await collector._scrape_public_items(
        "https://story.snapchat.com/@alice", "alice",
    )

    assert calls == [
        "https://story.snapchat.com/@alice",
        "https://www.snapchat.com/add/alice",
    ]
    assert final_url == "https://www.snapchat.com/add/alice"
    assert len(items) == 1
    assert items[0]["metadata"]["content_type"] == "highlight"


@pytest.mark.asyncio
async def test_snapchat_native_scrape_does_not_fallback_after_rate_limit(monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        raise _PublicPageHttpError(429, 900)

    monkeypatch.setattr(
        snapchat_mod.SnapchatCollector,
        "_fetch_public_page",
        staticmethod(fetch),
    )
    collector = object.__new__(snapchat_mod.SnapchatCollector)

    with pytest.raises(_PublicPageHttpError) as error:
        await collector._scrape_public_items(
            "https://story.snapchat.com/@alice", "alice",
        )

    assert error.value.retry_after_seconds == 900
    assert calls == ["https://story.snapchat.com/@alice"]


@pytest.mark.asyncio
async def test_snapchat_native_scrape_stops_on_explicit_verified_badge(monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        return '<div aria-label="Verified account"></div>'

    monkeypatch.setattr(
        snapchat_mod.SnapchatCollector,
        "_fetch_public_page",
        staticmethod(fetch),
    )
    collector = object.__new__(snapchat_mod.SnapchatCollector)

    with pytest.raises(RuntimeError, match="verified_account_excluded"):
        await collector._scrape_public_items(
            "https://story.snapchat.com/@alice", "alice",
        )

    assert calls == ["https://story.snapchat.com/@alice"]


def test_snapchat_item_cap_gives_each_content_type_a_bounded_share():
    story_snaps = [
        {
            "snapId": {"value": f"story-{index}"},
            "snapUrls": {"mediaUrl": f"https://cf-st.sc-cdn.net/story-{index}.mp4"},
        }
        for index in range(25)
    ]
    payload = {"props": {"pageProps": {
        "story": {"snapList": story_snaps},
        "curatedHighlights": [{
            "highlightId": {"value": "highlight-1"},
            "snapList": [{
                "snapId": {"value": "highlight-snap"},
                "snapUrls": {"mediaUrl": "https://cf-st.sc-cdn.net/highlight.mp4"},
            }],
        }],
        "spotlightHighlights": [{
            "spotlightId": {"value": "spotlight-1"},
            "snapList": [{
                "snapId": {"value": "spotlight-snap"},
                "snapUrls": {"mediaUrl": "https://cf-st.sc-cdn.net/spotlight.mp4"},
            }],
        }],
    }}}
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )

    items = _extract_public_snap_items(
        html, "https://story.snapchat.com/@alice", "alice",
    )

    types = [item["metadata"]["content_type"] for item in items]
    assert len(items) == 25
    assert types.count("public_story") == 23
    assert types.count("highlight") == 1
    assert types.count("spotlight") == 1


@pytest.mark.parametrize("payload", ["[]", "null", "{\"props\":null}"])
def test_snapchat_story_page_parser_ignores_unexpected_next_data_shapes(payload):
    html = f'<script id="__NEXT_DATA__" type="application/json">{payload}</script>'

    assert _extract_public_snap_items(
        html, "https://story.snapchat.com/@alice", "alice",
    ) == []


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
