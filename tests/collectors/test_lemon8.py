"""Tests for src/collectors/lemon8.py — Wave 2 hardened port.

Pure-unit. httpx is patched at the module level — no real network. asyncpg
pool replaced with AsyncMock. Lemon8 shares anti-bot infra with TikTok so
this suite *must not* hit the network.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.collectors import lemon8 as lemon8_mod
from src.collectors.lemon8 import (
    Lemon8Collector,
    Lemon8EdgeFetcher,
    _enhance_image_url,
    _safe_log_text,
)


# ── fixtures ──────────────────────────────────────────────────────────────


def _make_pool():
    """AsyncMock-backed asyncpg pool stand-in.

    `async with self.pool.acquire() as conn:` works because acquire()
    returns an async context manager whose __aenter__ yields a conn mock.
    """
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool, conn


@pytest.fixture
def collector(tmp_path, monkeypatch):
    # Disable optional features that would touch state we don't need.
    monkeypatch.delenv("LEMON8_COOKIES_FILE", raising=False)
    monkeypatch.setenv("LEMON8_FEED_ENABLED", "false")
    monkeypatch.setenv("LEMON8_PROFILE_PHOTO_ENABLED", "false")
    monkeypatch.setenv("LEMON8_SPIDER_ENABLED", "false")
    # Redirect media_dir into tmp_path so build/save calls don't escape.
    monkeypatch.setattr(
        "src.core.base_collector.DRIVE_PATH", str(tmp_path),
    )
    c = Lemon8Collector()
    pool, conn = _make_pool()
    c.set_pool(pool)
    # Stash the conn for assertions.
    c._test_conn = conn  # type: ignore[attr-defined]
    # Avoid the async rate limiter actually sleeping.
    c.rate_limiter.async_wait = AsyncMock()
    return c


# ── _enhance_image_url ────────────────────────────────────────────────────


def test_enhance_image_url_strips_shrink_params():
    url = "https://cdn.lemon8.com/img/abc.jpg?w=320&h=320&q=70"
    out = _enhance_image_url(url)
    assert "w=320" not in out
    assert "h=320" not in out
    assert "q=70" not in out


def test_enhance_image_url_strips_path_segments():
    url = "https://cdn.lemon8.com/w:200/h:200/thumb/100x100/abc.jpg"
    out = _enhance_image_url(url)
    assert "w:200" not in out
    assert "h:200" not in out
    assert "thumb/100x100" not in out


def test_enhance_image_url_strips_tplv_template():
    url = "https://cdn.lemon8.com/abc~tplv-abc12-img.jpeg"
    out = _enhance_image_url(url)
    assert "tplv" not in out


def test_safe_log_text_redacts_signed_query_strings():
    msg = (
        "Client error for url "
        "'https://p16-common-sign.tiktokcdn.com/avatar.jpg?refresh_token=abc&x-signature=secret'"
    )
    out = _safe_log_text(msg)
    assert "refresh_token" not in out
    assert "x-signature" not in out
    assert "https://p16-common-sign.tiktokcdn.com/avatar.jpg?<redacted>" in out


# ── constructor / config ──────────────────────────────────────────────────


def test_constructor_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("LEMON8_COOKIES_FILE", raising=False)
    monkeypatch.setattr(
        "src.core.base_collector.DRIVE_PATH", str(tmp_path),
    )
    c = Lemon8Collector()
    assert c.SOURCE_NAME == "lemon8"
    assert c.USE_HUMAN_RATE_LIMITER is True
    assert c._cookies == {}
    assert c._discovered_users == set()
    assert c._discovered_tags == set()


def test_constructor_loads_cookies_from_netscape_file(tmp_path, monkeypatch):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        "\n"
        ".lemon8-app.com\tTRUE\t/\tFALSE\t0\tsessionid\tabc123\n"
        ".lemon8-app.com\tTRUE\t/\tFALSE\t0\ttt_webid\txyz789\n"
    )
    monkeypatch.setenv("LEMON8_COOKIES_FILE", str(cookie_file))
    monkeypatch.setattr(
        "src.core.base_collector.DRIVE_PATH", str(tmp_path),
    )
    c = Lemon8Collector()
    assert c._cookies == {"sessionid": "abc123", "tt_webid": "xyz789"}


def test_parse_cookies_silent_on_missing_file():
    out = Lemon8Collector._parse_cookies("/nonexistent/path/cookies.txt")
    assert out == {}


def test_parse_cookies_skips_short_lines(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text("not\tenough\tfields\n# comment\n\n")
    out = Lemon8Collector._parse_cookies(str(f))
    assert out == {}


def test_account_media_dir_uses_default_when_no_cookies(collector, tmp_path):
    p = collector.account_media_dir
    assert p.exists()
    assert "account_default" in p.name


def test_account_media_dir_uses_cookie_stem(tmp_path, monkeypatch):
    cookie_file = tmp_path / "myacct.txt"
    cookie_file.write_text("# empty\n")
    monkeypatch.setenv("LEMON8_COOKIES_FILE", str(cookie_file))
    monkeypatch.setattr(
        "src.core.base_collector.DRIVE_PATH", str(tmp_path),
    )
    c = Lemon8Collector()
    p = c.account_media_dir
    assert "myacct" in p.name


def test_headers_includes_lemon8_referer(collector):
    h = collector._headers()
    assert h["Referer"] == "https://www.lemon8-app.com/"
    assert h["Accept"] == "application/json"
    assert "User-Agent" in h


@pytest.mark.asyncio
async def test_collect_runs_explicit_targets_before_feed(monkeypatch, collector):
    monkeypatch.setenv("LEMON8_SPIDER_ENABLED", "false")
    collector._feed_enabled = True
    events = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(lemon8_mod.httpx, "AsyncClient", lambda *args, **kwargs: _Client())

    async def _collect_user(_client, username):
        events.append(("target", username))

    async def _collect_feed(_client):
        events.append(("feed", "feed"))

    monkeypatch.setattr(collector, "_collect_user", _collect_user)
    monkeypatch.setattr(collector, "_collect_feed", _collect_feed)

    await collector.collect(["alice"])

    assert events == [("target", "alice"), ("feed", "feed")]


@pytest.mark.asyncio
async def test_collect_limits_configured_targets_per_cycle(monkeypatch, collector):
    monkeypatch.setenv("LEMON8_SPIDER_ENABLED", "false")
    collector._feed_enabled = False
    collector._target_limit_per_cycle = 1
    seen = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(lemon8_mod.httpx, "AsyncClient", lambda *args, **kwargs: _Client())

    async def _collect_user(_client, username):
        seen.append(username)

    monkeypatch.setattr(collector, "_collect_user", _collect_user)

    await collector.collect(["alice", "bob", "carol"])

    assert seen == ["alice"]


@pytest.mark.asyncio
async def test_collect_rotates_configured_targets_after_checkpoint(monkeypatch, collector):
    monkeypatch.setenv("LEMON8_SPIDER_ENABLED", "false")
    collector._feed_enabled = False
    collector._target_limit_per_cycle = 2
    collector.checkpoint.last_processed_id = "bob"
    seen = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(lemon8_mod.httpx, "AsyncClient", lambda *args, **kwargs: _Client())

    async def _collect_user(_client, username):
        seen.append(username)

    monkeypatch.setattr(collector, "_collect_user", _collect_user)

    await collector.collect(["alice", "bob", "carol"])

    assert seen == ["carol", "alice"]


@pytest.mark.asyncio
async def test_collect_saves_progress_for_successful_tag(monkeypatch, collector):
    monkeypatch.setenv("LEMON8_SPIDER_ENABLED", "false")
    collector._feed_enabled = False
    collector._target_limit_per_cycle = 0
    collector._collect_tag = AsyncMock()
    collector.checkpoint.save_progress = AsyncMock()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(lemon8_mod.httpx, "AsyncClient", lambda *args, **kwargs: _Client())

    await collector.collect(["#singapore"])

    collector._collect_tag.assert_awaited_once()
    collector.checkpoint.save_progress.assert_awaited_once_with("#singapore")


@pytest.mark.asyncio
async def test_process_spider_queue_respects_cycle_limit(monkeypatch, collector):
    conn = collector._test_conn  # type: ignore[attr-defined]
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"platform_user_id": "alice"},
            {"platform_user_id": "bob"},
            {"platform_user_id": "carol"},
        ]
    )

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(lemon8_mod.httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    collector._collect_user = AsyncMock()

    await collector._process_spider_queue(max_items=2)

    assert collector._collect_user.await_count == 2
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_collect_feed_dedupes_and_caps_detail_fetches(monkeypatch, collector):
    monkeypatch.setenv("LEMON8_FYP_DETAIL_FETCH", "true")
    monkeypatch.setattr(lemon8_mod, "PYLEMON8_AVAILABLE", False)
    collector._feed_media_per_cycle = 10
    collector._fyp_detail_per_cycle = 1
    collector.is_known = MagicMock(return_value=True)
    collector._link_lemon8_media = AsyncMock()
    collector._fetch_note_detail = AsyncMock(return_value={"platform_post_id": "111", "media": []})
    collector._upsert_post = AsyncMock(return_value=True)
    collector.download_media = AsyncMock()

    async def _scrape_feed_with_web(_client, _pages):
        return {
            "media_items": [
                {"url": "https://cdn.lemon8.test/1.jpg", "username": "alice", "note_id": "111"},
                {"url": "https://cdn.lemon8.test/2.jpg", "username": "alice", "note_id": "111"},
                {"url": "https://cdn.lemon8.test/3.jpg", "username": "bob", "note_id": "222"},
            ],
            "discovered_users": [],
            "discovered_tags": [],
        }

    monkeypatch.setattr(collector, "_scrape_feed_with_web", _scrape_feed_with_web)

    await collector._collect_feed(object())

    collector._fetch_note_detail.assert_awaited_once()
    collector._upsert_post.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_skips_unavailable_profile_without_dlq(monkeypatch, collector):
    monkeypatch.setenv("LEMON8_SPIDER_ENABLED", "false")
    collector.send_to_dlq = AsyncMock()
    collector.checkpoint.save_progress = AsyncMock()

    request = httpx.Request("GET", "https://www.lemon8-app.com/@missing")
    response = httpx.Response(404, request=request)
    error = httpx.HTTPStatusError("not found", request=request, response=response)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(lemon8_mod.httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    monkeypatch.setattr(collector, "_collect_user", AsyncMock(side_effect=error))

    await collector.collect(["missing"])

    collector.send_to_dlq.assert_not_awaited()
    collector.checkpoint.save_progress.assert_awaited_once_with("missing")
    collector._test_conn.execute.assert_awaited_once()
    sql, source, target, reason = collector._test_conn.execute.await_args.args
    assert "UPDATE collection_targets" in sql
    assert "status = 'unavailable'" in sql
    assert "preserve_on_source_config_sync" in sql
    assert "status IN ('pending', 'error', 'active')" not in sql
    assert "COALESCE(status, 'pending') <> 'disabled'" in sql
    assert source == "lemon8"
    assert target == "missing"
    assert reason == "http_404"


@pytest.mark.asyncio
async def test_record_http_status_event_persists_429(monkeypatch, collector):
    events = []

    async def record_event(pool, **kwargs):
        events.append((pool, kwargs))

    async def record_cooldown(*args, **kwargs):
        return SimpleNamespace(
            seconds_remaining=123,
            service="rate_limit:lemon8:profile:lemon8_default",
            streak=1,
            scope="profile",
        )

    sleep = AsyncMock()
    monkeypatch.setattr(lemon8_mod, "record_rate_limit_event", record_event)
    monkeypatch.setattr(lemon8_mod, "record_dynamic_cooldown", record_cooldown)
    monkeypatch.setattr(lemon8_mod, "sleep_rate_limit", sleep)
    collector._rate_limit_cooldown_seconds = 123

    request = httpx.Request(
        "GET",
        "https://www.lemon8-app.com/@alice?session_secret=should_not_persist",
    )
    response = httpx.Response(429, request=request)
    error = httpx.HTTPStatusError("too many requests", request=request, response=response)

    recorded = await collector._record_http_status_event(
        error,
        scope="profile_fetch",
        subject="alice",
        url=str(request.url),
    )

    assert recorded is True
    assert len(events) == 1
    _, kwargs = events[0]
    assert kwargs["source"] == "lemon8"
    assert kwargs["account"] == "lemon8_default"
    assert kwargs["scope"] == "profile_fetch"
    assert kwargs["status_code"] == 429
    assert kwargs["cooldown_seconds"] == 123
    assert kwargs["metadata"]["subject"] == "alice"
    assert kwargs["metadata"]["url_host"] == "www.lemon8-app.com"
    assert kwargs["metadata"]["url_path"] == "/@alice"
    assert "session_secret" not in str(kwargs["metadata"])
    sleep.assert_awaited_once_with(123)


@pytest.mark.asyncio
async def test_record_http_status_event_ignores_media_403(monkeypatch, collector):
    event = AsyncMock()
    monkeypatch.setattr(lemon8_mod, "record_rate_limit_event", event)

    request = httpx.Request("GET", "https://cdn.lemon8-app.com/avatar.jpg")
    response = httpx.Response(403, request=request)
    error = httpx.HTTPStatusError("forbidden", request=request, response=response)

    recorded = await collector._record_http_status_event(
        error,
        scope="media_download",
        subject="alice",
        url=str(request.url),
        record_access_errors=False,
    )

    assert recorded is False
    event.assert_not_awaited()


# ── _resolve_post_id ──────────────────────────────────────────────────────


def test_resolve_post_id_prefers_explicit_id():
    pid = Lemon8Collector._resolve_post_id({"id": "12345"})
    assert pid == "12345"


def test_resolve_post_id_prefers_other_known_keys():
    for k in ("itemId", "note_id", "card_id", "post_id", "aweme_id"):
        assert Lemon8Collector._resolve_post_id({k: "abc"}) == "abc"


def test_resolve_post_id_synthesizes_from_url():
    pid = Lemon8Collector._resolve_post_id(
        {"share_url": "https://www.lemon8-app.com/post/xyz"}
    )
    assert pid is not None
    assert pid.startswith("fyp_")


def test_resolve_post_id_synthesizes_from_media():
    pid = Lemon8Collector._resolve_post_id(
        {"media": [{"url": "https://cdn.lemon8.com/abc.jpg"}]}
    )
    assert pid is not None
    assert pid.startswith("fyp_")


def test_resolve_post_id_ignores_falsy_id_strings():
    # "none"/"null"/"0" should NOT be treated as valid platform ids.
    pid = Lemon8Collector._resolve_post_id(
        {"id": "null", "share_url": "https://x.com/y"},
    )
    assert pid is not None
    assert pid.startswith("fyp_")


# ── _normalize_username ───────────────────────────────────────────────────


def test_normalize_username_strips_at_sign(collector):
    assert collector._normalize_username("@Foo.Bar") == "foo.bar"


def test_normalize_username_returns_none_for_empty(collector):
    assert collector._normalize_username("") is None
    assert collector._normalize_username(None) is None
    assert collector._normalize_username("@@@") is None


def test_normalize_username_strips_disallowed_chars(collector):
    assert collector._normalize_username("user!@#name") == "username"


# ── _is_valid_media_url / _is_small_image ─────────────────────────────────


def test_is_valid_media_url_rejects_non_http(collector):
    assert collector._is_valid_media_url("ftp://x/y.jpg") is False
    assert collector._is_valid_media_url("") is False
    assert collector._is_valid_media_url(None) is False  # type: ignore[arg-type]


def test_is_valid_media_url_rejects_static_assets(collector):
    assert collector._is_valid_media_url("https://x.com/icon.svg") is False
    assert collector._is_valid_media_url("https://x.com/main.css") is False
    assert collector._is_valid_media_url("https://x.com/sdk-web/main.js") is False


def test_is_valid_media_url_accepts_jpeg(collector):
    assert collector._is_valid_media_url(
        "https://cdn.lemon8.com/posts/abc.jpg"
    ) is True


# ── _extract_avatar ───────────────────────────────────────────────────────


def test_extract_avatar_finds_avatar_url(collector):
    html = '...{"avatar_url":"https://cdn.lemon8.com/avatar/u.jpg"}...'
    url = collector._extract_avatar(html)
    assert url is not None
    assert url.startswith("https://cdn.lemon8.com/avatar")


def test_extract_avatar_handles_unicode_slash(collector):
    html = '..."avatarUrl":"https:\\u002F\\u002Fcdn.lemon8.com\\u002Favatar\\u002Fu.jpg"...'
    url = collector._extract_avatar(html)
    assert url is not None
    assert url.startswith("https://cdn.lemon8.com")


def test_extract_avatar_returns_none_when_missing(collector):
    assert collector._extract_avatar("<html>nothing here</html>") is None


# ── _upsert_profile / _upsert_post (DB pool) ──────────────────────────────


@pytest.mark.asyncio
async def test_upsert_profile_calls_db(collector):
    await collector._upsert_profile(
        "uid-1", "alice",
        {"nickname": "Alice", "avatar_url": "https://x/y.jpg"},
    )
    calls = [call.args for call in collector._test_conn.execute.await_args_list]
    insert = next(args for args in calls if "INSERT INTO lemon8_profiles" in args[0])
    assert any("UPDATE lemon8_profiles SET platform_user_id" in args[0] for args in calls)
    # positional params include user_id and username
    assert "uid-1" in insert
    assert "alice" in insert


@pytest.mark.asyncio
async def test_upsert_post_skips_when_no_resolvable_id(collector):
    # post_data with truly nothing resolvable: even json.dumps fallback
    # produces a synth id, so to test the skip path use empty dict and
    # patch _resolve_post_id to return None.
    with patch.object(Lemon8Collector, "_resolve_post_id", return_value=None):
        await collector._upsert_post("uid-1", {})
    collector._test_conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_post_writes_post_row(collector):
    post = {
        "id": "post-9",
        "title": "hi",
        "description": "desc",
        "stats": {"likeCount": 5, "commentCount": 2},
        "media": [
            {"url": "https://cdn/a.jpg", "media_type": "image"},
            {"url": "https://cdn/b.mp4", "media_type": "video"},
        ],
    }
    await collector._upsert_post("uid-1", post)
    assert any(
        "INSERT INTO lemon8_posts" in call.args[0]
        for call in collector._test_conn.execute.await_args_list
    )


# ── collect / collect_user_profile / collect_user_posts ───────────────────


def _make_httpx_response(*, text: str = "", status_code: int = 200, content: bytes = b""):
    resp = MagicMock()
    resp.text = text
    resp.content = content
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


def _patch_async_client(monkeypatch, *, get_response=None, get_raises=None):
    """Replace lemon8.httpx.AsyncClient with a mock that yields a client
    whose .get() either returns ``get_response`` or raises ``get_raises``.
    Returns the client mock so tests can assert on call shape."""
    client = MagicMock()
    if get_raises is not None:
        client.get = AsyncMock(side_effect=get_raises)
    else:
        client.get = AsyncMock(
            return_value=get_response or _make_httpx_response()
        )
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(lemon8_mod.httpx, "AsyncClient", MagicMock(return_value=cm))
    return client


@pytest.mark.asyncio
async def test_download_media_writes_vault_blob(collector, monkeypatch, tmp_path):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(lemon8_mod, "VAULT_ROOT", vault_root)
    collector._min_file_size = 1
    collector.insert_media_item = AsyncMock(return_value=True)
    collector.send_to_dlq = AsyncMock()

    data = b"lemon8 image bytes"
    digest = hashlib.sha256(data).hexdigest()
    _patch_async_client(
        monkeypatch,
        get_response=_make_httpx_response(status_code=200, content=data),
    )

    result = await collector.download_media({
        "entity_id": "alice",
        "entity_name": "alice",
        "content_type": "photo",
        "content_id": "post1_img1",
        "extension": "jpg",
        "url": "https://cdn.lemon8-app.com/post1.jpg",
        "raw": {"post_id": "post1"},
    })

    assert result is True
    kwargs = collector.insert_media_item.await_args.kwargs
    stored_path = Path(kwargs["file_path"])
    assert stored_path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    assert stored_path.read_bytes() == data
    assert kwargs["sha256"] == digest
    assert kwargs["file_size"] == len(data)
    assert kwargs["source_url"] == "https://cdn.lemon8-app.com/post1.jpg"
    assert kwargs["metadata"]["raw"] == {"post_id": "post1"}
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    assert kwargs["metadata"]["vault_artifact"]["partial"] is False
    assert kwargs["metadata"]["vault_artifact"]["blob_path"].startswith("media/blobs/")
    collector.send_to_dlq.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_media_ignores_permanent_cdn_403_without_dlq(collector, monkeypatch):
    collector._min_file_size = 1
    collector.insert_media_item = AsyncMock()
    collector.send_to_dlq = AsyncMock()

    request = httpx.Request("GET", "https://cdn.lemon8-app.com/avatar.jpg")
    response = httpx.Response(403, request=request)
    error = httpx.HTTPStatusError("forbidden", request=request, response=response)
    _patch_async_client(monkeypatch, get_raises=error)

    result = await collector.download_media({
        "entity_id": "alice",
        "entity_name": "alice",
        "content_type": "photo",
        "content_id": "profile_alice",
        "extension": "jpg",
        "url": "https://cdn.lemon8-app.com/avatar.jpg",
    })

    assert result is False
    collector.insert_media_item.assert_not_awaited()
    collector.send_to_dlq.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_media_returns_false_for_duplicate_insert(collector, monkeypatch, tmp_path):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(lemon8_mod, "VAULT_ROOT", vault_root)
    collector._min_file_size = 1
    collector.insert_media_item = AsyncMock(return_value=False)
    collector.send_to_dlq = AsyncMock()

    _patch_async_client(
        monkeypatch,
        get_response=_make_httpx_response(status_code=200, content=b"duplicate image bytes"),
    )

    result = await collector.download_media({
        "entity_id": "alice",
        "entity_name": "alice",
        "content_type": "photo",
        "content_id": "post1_img1",
        "extension": "jpg",
        "url": "https://cdn.lemon8-app.com/post1.jpg",
    })

    assert result is False
    assert "post1_img1" not in collector._known_ids
    collector.send_to_dlq.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_user_profile_returns_none_for_empty_username(collector):
    assert await collector.collect_user_profile("") is None
    assert await collector.collect_user_profile(None) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_collect_user_profile_returns_none_on_http_failure(
    collector, monkeypatch, caplog,
):
    _patch_async_client(monkeypatch, get_raises=RuntimeError("boom"))
    with caplog.at_level("WARNING", logger="src.collectors.lemon8"):
        out = await collector.collect_user_profile("alice")
    assert out is None
    assert any("alice" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_collect_user_profile_records_access_denial(
    collector, monkeypatch,
):
    resp = _make_httpx_response(status_code=403)
    resp.raise_for_status.side_effect = RuntimeError("HTTP 403")
    _patch_async_client(monkeypatch, get_response=resp)
    monkeypatch.setattr(collector, "_record_profile_access", AsyncMock())

    out = await collector.collect_user_profile("alice")

    assert out is None
    collector._record_profile_access.assert_awaited_once_with(
        "alice", False, error="HTTP 403",
    )


@pytest.mark.asyncio
async def test_collect_user_profile_happy_path(collector, monkeypatch):
    html = '{"user_id":"uid-42","avatar_url":"https://cdn/x.jpg"}'
    _patch_async_client(
        monkeypatch, get_response=_make_httpx_response(text=html),
    )
    monkeypatch.setattr(collector, "_record_profile_access", AsyncMock())
    out = await collector.collect_user_profile("@alice")
    assert out is not None
    assert out["user_id"] == "uid-42"
    assert out["username"] == "alice"
    collector._record_profile_access.assert_awaited_once_with("alice", True)
    # _upsert_profile invoked → conn.execute awaited at least once.
    collector._test_conn.execute.assert_awaited()


@pytest.mark.asyncio
async def test_collect_user_posts_returns_empty_for_empty_username(collector):
    assert await collector.collect_user_posts("") == []


@pytest.mark.asyncio
async def test_collect_user_posts_returns_empty_on_http_failure(
    collector, monkeypatch,
):
    _patch_async_client(monkeypatch, get_raises=RuntimeError("net dead"))
    out = await collector.collect_user_posts("alice")
    assert out == []


@pytest.mark.asyncio
async def test_collect_user_posts_returns_list(collector, monkeypatch):
    # Empty/no-script HTML → _extract_posts returns [] (no bs4 parse hits).
    _patch_async_client(
        monkeypatch, get_response=_make_httpx_response(text="<html></html>"),
    )
    monkeypatch.setattr(collector, "_record_profile_access", AsyncMock())
    out = await collector.collect_user_posts("alice")
    assert out == []
    collector._record_profile_access.assert_awaited_once_with("alice", True)


@pytest.mark.asyncio
async def test_get_backfill_items_skips_avatar_backfill_during_cooldown(
    collector, monkeypatch,
):
    collector._profile_photos = True
    monkeypatch.setattr(
        collector,
        "_cooldown_active_for_scope",
        AsyncMock(return_value=True),
    )

    assert await collector.get_backfill_items(10) == []
    collector._test_conn.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_collect_following_yields_handles(collector, monkeypatch):
    html = "irrelevant — handles come from _extract_user_handles patch"
    _patch_async_client(
        monkeypatch, get_response=_make_httpx_response(text=html),
    )
    monkeypatch.setattr(
        Lemon8Collector, "_extract_user_handles",
        lambda self, h: {"bob", "carol", "alice"},  # alice == seed → filtered
    )
    monkeypatch.setattr(
        Lemon8Collector, "_enqueue_spider_user",
        AsyncMock(),
    )
    yielded = [u async for u in collector.collect_following("@alice")]
    # alice (seed, lowercased) should be filtered out.
    assert "alice" not in yielded
    assert set(yielded) == {"bob", "carol"}


@pytest.mark.asyncio
async def test_collect_following_returns_empty_for_empty_username(collector):
    yielded = [u async for u in collector.collect_following("")]
    assert yielded == []


@pytest.mark.asyncio
async def test_collect_following_swallows_http_error(collector, monkeypatch):
    _patch_async_client(monkeypatch, get_raises=RuntimeError("503"))
    yielded = [u async for u in collector.collect_following("alice")]
    assert yielded == []


# ── spider_related_creators ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spider_related_creators_no_pool_returns_zero(collector):
    collector.pool = None  # explicit
    n = await collector.spider_related_creators("alice")
    assert n == 0


@pytest.mark.asyncio
async def test_spider_related_creators_empty_seed_returns_zero(collector):
    assert await collector.spider_related_creators("") == 0
    assert await collector.spider_related_creators("@") == 0


@pytest.mark.asyncio
async def test_spider_related_creators_runs_spider(collector, monkeypatch):
    fake_spider = MagicMock()
    fake_spider.run = AsyncMock(return_value=7)
    monkeypatch.setattr(
        Lemon8Collector, "make_spider_discover",
        lambda self, *, max_hops=None: fake_spider,
    )
    # Ensure SpiderDiscover is treated as importable.
    monkeypatch.setattr(lemon8_mod, "SpiderDiscover", object)
    n = await collector.spider_related_creators("@alice")
    assert n == 7
    fake_spider.run.assert_awaited_once_with(seeds=["alice"])


@pytest.mark.asyncio
async def test_spider_related_creators_swallows_init_error(
    collector, monkeypatch,
):
    def boom(self, *, max_hops=None):
        raise RuntimeError("init failed")
    monkeypatch.setattr(Lemon8Collector, "make_spider_discover", boom)
    monkeypatch.setattr(lemon8_mod, "SpiderDiscover", object)
    n = await collector.spider_related_creators("alice")
    assert n == 0


# ── EdgeFetcher ───────────────────────────────────────────────────────────


def test_make_edge_fetcher_returns_correct_type(collector):
    f = collector.make_edge_fetcher()
    assert isinstance(f, Lemon8EdgeFetcher)
    assert f._c is collector


@pytest.mark.asyncio
async def test_edge_fetcher_no_op_when_spider_unavailable(
    collector, monkeypatch,
):
    monkeypatch.setattr(lemon8_mod, "Edge", None)
    monkeypatch.setattr(lemon8_mod, "EdgeType", None)
    f = Lemon8EdgeFetcher(collector)
    # collect_following won't even be called — generator returns immediately.
    out = [e async for e in f.fetch_edges("alice", "FOLLOWING")]
    assert out == []


# ── cleanup ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_is_noop(collector):
    # Just must not raise.
    assert await collector.cleanup() is None
