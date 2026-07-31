"""Tests for src/collectors/tiktok.py — Wave 2 hardened TikTok collector.

Pure-unit. No subprocess (gallery-dl/yt-dlp), no httpx network, no playwright,
no real DB pool. All side-effecting paths are stubbed.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Force a writable drive path BEFORE importing the collector.
os.environ.setdefault(
    "COLLECTOR_DRIVE_PATH",
    os.path.join(os.environ.get("TEMP", "/tmp"), "uc_test_media_tt"),
)

from src.collectors import tiktok as tiktok_mod  # noqa: E402
from src.collectors.tiktok import (  # noqa: E402
    InvalidReason,
    TiktokCollector,
    TiktokEdgeFetcher,
    ValidationResult,
    classify_invalid_username,
    validate_cookies,
    validate_username,
)


# ── fake pool ─────────────────────────────────────────────────────────────


def _make_pool(*, fetchrow_returns=None, fetch_returns=None, executes_raise=False):
    conn = MagicMock()
    if executes_raise:
        conn.execute = AsyncMock(side_effect=RuntimeError("db down"))
    else:
        conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=fetchrow_returns)
    conn.fetch = AsyncMock(return_value=fetch_returns or [])

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool._conn = conn
    return pool


# ── module helpers ────────────────────────────────────────────────────────


def test_validate_username_strips_at_and_passes_alphanum():
    assert validate_username("@bryan") == "bryan"
    assert validate_username("user_name.1-x") == "user_name.1-x"


def test_validate_username_rejects_bad_chars():
    with pytest.raises(ValueError):
        validate_username("bad name!")
    with pytest.raises(ValueError):
        validate_username("")
    with pytest.raises(ValueError):
        validate_username("@")
    with pytest.raises(ValueError):
        validate_username("a" * 31)
    with pytest.raises(ValueError):
        validate_username(123)  # type: ignore[arg-type]


def test_classify_rate_limit_by_status_and_text():
    r = classify_invalid_username("got rate limit", http_status=200)
    assert r.is_rate_limited and r.should_retry and r.is_valid

    r2 = classify_invalid_username("err", http_status=429)
    assert r2.is_rate_limited and r2.should_retry


def test_classify_not_found_by_404():
    r = classify_invalid_username(None, http_status=404)
    assert r.is_valid is False
    assert r.invalid_reason == InvalidReason.NOT_FOUND


def test_classify_private_banned_by_text():
    r = classify_invalid_username("This account is private", http_status=200)
    assert r.is_valid is False
    assert r.invalid_reason == InvalidReason.PRIVATE_BANNED


def test_classify_account_deleted_by_text():
    r = classify_invalid_username("Account has been deleted")
    assert r.is_valid is False
    assert r.invalid_reason == InvalidReason.ACCOUNT_DELETED


def test_classify_username_changed_by_text():
    r = classify_invalid_username("username changed last week")
    assert r.is_valid is False
    assert r.invalid_reason == InvalidReason.USERNAME_CHANGED


def test_classify_5xx_is_network_retry():
    r = classify_invalid_username("upstream", http_status=503)
    assert r.is_valid is True
    assert r.is_network_error and r.should_retry


def test_validate_cookies_missing_file(tmp_path):
    out = validate_cookies(str(tmp_path / "missing.txt"))
    assert out["valid"] is False
    assert any("not found" in w.lower() for w in out["warnings"])


def test_validate_cookies_happy_path(tmp_path):
    cookies = tmp_path / "cookies.txt"
    # Netscape format: domain, flag, path, secure, expiry, name, value (tab-separated)
    far_future = "9999999999"
    lines = []
    for name in ("sessionid", "tt_csrf_token", "ttwid", "msToken",
                 "tt_chain_token", "sid_guard"):
        lines.append(f".tiktok.com\tTRUE\t/\tTRUE\t{far_future}\t{name}\tval-{name}")
    cookies.write_text("\n".join(lines) + "\n")

    out = validate_cookies(str(cookies))
    assert out["valid"] is True
    assert out["total"] == 6
    assert not out["missing"]


def test_validate_cookies_expired_required(tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(".tiktok.com\tTRUE\t/\tTRUE\t1\tsessionid\tabc\n")
    out = validate_cookies(str(cookies))
    assert out["valid"] is False
    # sessionid is expired AND missing other required cookies → warnings.
    assert "sessionid" in out["expired"]


# ── constructor ───────────────────────────────────────────────────────────


def test_constructor_defaults(monkeypatch):
    for var in ("TIKTOK_COOKIES_FILE", "TIKTOK_SESSION_ID",
                "TIKTOK_BROWSER_FALLBACK_ENABLED", "TIKTOK_YTDLP_FALLBACK_ENABLED",
                "TIKTOK_VIDEO_FOLLOWER_CAP", "TIKTOK_PROFILE_CHUNK_ITEMS",
                "TIKTOK_GALLERY_DL_MAX_ITEMS", "TIKTOK_YTDLP_MAX_DOWNLOADS"):
        monkeypatch.delenv(var, raising=False)
    # Force the tool-availability probe to "no tools" for deterministic boot,
    # and disable cookie auto-discovery so on-disk credentials don't leak in.
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)), \
         patch.object(TiktokCollector, "_discover_cookie_file", staticmethod(lambda: "")):
        c = TiktokCollector()
    assert c.SOURCE_NAME == "tiktok"
    assert c._cookies_file == ""
    assert c._cookies_valid is False
    assert c._use_gallery_dl is False
    assert c._use_yt_dlp is False
    assert c._browser_fallback is True
    assert c._ytdlp_fallback is True
    assert c._video_follower_cap == 300
    assert c._profile_chunk_items == 60


def test_constructor_with_invalid_cookies_logs_warnings(tmp_path, monkeypatch, caplog):
    bad_cookies = tmp_path / "creds.txt"
    bad_cookies.write_text(
        ".tiktok.com\tTRUE\t/\tTRUE\t1\tsessionid\tabc\n"
    )
    monkeypatch.setenv("TIKTOK_COOKIES_FILE", str(bad_cookies))
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        with caplog.at_level("WARNING", logger="src.collectors.tiktok"):
            c = TiktokCollector()
    assert c._cookies_file == str(bad_cookies)
    assert c._cookies_valid is False


def test_check_tool_returns_false_for_missing():
    # 'definitely-not-a-real-binary-xyz' will not be found.
    assert TiktokCollector._check_tool("definitely-not-a-real-binary-xyz") is False


def test_gallery_dl_archive_args_are_per_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOK_GALLERY_DL_ARCHIVE_DIR", str(tmp_path / "archives"))
    monkeypatch.setenv("TIKTOK_GALLERY_DL_RANGE_DIR", str(tmp_path / "ranges"))
    monkeypatch.setenv("TIKTOK_GALLERY_DL_ARCHIVE_ENABLED", "true")
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)), \
         patch.object(TiktokCollector, "_discover_cookie_file", staticmethod(lambda: "")):
        c = TiktokCollector()

    args = c._gallery_dl_archive_args("bad/name user")

    assert args[0] == "--download-archive"
    assert Path(args[1]).parent == tmp_path / "archives"
    assert Path(args[1]).name.endswith(".txt")
    assert "/" not in Path(args[1]).name
    assert Path(args[1]).parent.exists()
    assert args[-2:] == ["--range", "1-60"]


def test_gallery_dl_range_cursor_advances_between_cycles(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOK_GALLERY_DL_ARCHIVE_DIR", str(tmp_path / "archives"))
    monkeypatch.setenv("TIKTOK_GALLERY_DL_RANGE_DIR", str(tmp_path / "ranges"))
    monkeypatch.setenv("TIKTOK_GALLERY_DL_MAX_ITEMS", "30")
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)), \
         patch.object(TiktokCollector, "_discover_cookie_file", staticmethod(lambda: "")):
        c = TiktokCollector()

    assert c._gallery_dl_archive_args("alice")[-2:] == ["--range", "1-30"]
    c._advance_gallery_dl_range_cursor("alice", file_count=4, ok=True)

    assert c._gallery_dl_archive_args("alice")[-2:] == ["--range", "31-60"]


def test_profile_backoff_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOK_PROFILE_BACKOFF_DIR", str(tmp_path / "backoff"))
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)), \
         patch.object(TiktokCollector, "_discover_cookie_file", staticmethod(lambda: "")):
        c = TiktokCollector()

    c._record_profile_backoff("bad/name user", reason="timeout", seconds=60)

    remaining = c._profile_backoff_remaining("bad/name user")
    assert 0 < remaining <= 60
    assert c._profile_backoff_state_path("bad/name user").exists()


def test_gallery_dl_archive_args_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOK_GALLERY_DL_ARCHIVE_DIR", str(tmp_path / "archives"))
    monkeypatch.setenv("TIKTOK_GALLERY_DL_ARCHIVE_ENABLED", "false")
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)), \
         patch.object(TiktokCollector, "_discover_cookie_file", staticmethod(lambda: "")):
        c = TiktokCollector()

    assert c._gallery_dl_archive_args("alice") == []


def test_account_media_dir_uses_default_when_no_cookies(monkeypatch):
    monkeypatch.delenv("TIKTOK_COOKIES_FILE", raising=False)
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)), \
         patch.object(TiktokCollector, "_discover_cookie_file", staticmethod(lambda: "")):
        c = TiktokCollector()
    p = c.account_media_dir
    assert p.name == "default"
    assert p.exists()


# ── _is_invalid_username ──────────────────────────────────────────────────


def test_is_invalid_username_true_for_bad():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    assert c._is_invalid_username("bad name!") is True
    assert c._is_invalid_username("") is True


def test_is_invalid_username_false_for_good():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    assert c._is_invalid_username("bryan") is False


# ── _safe_int / _to_dt ────────────────────────────────────────────────────


def test_safe_int_handles_garbage():
    assert TiktokCollector._safe_int("12") == 12
    assert TiktokCollector._safe_int("") == 0
    assert TiktokCollector._safe_int(None) == 0
    assert TiktokCollector._safe_int("nope", default=99) == 99
    assert TiktokCollector._safe_int(7.4) == 7


def test_to_dt_unix_timestamp():
    dt = TiktokCollector._to_dt("1700000000")
    assert dt is not None
    assert dt.year == 2023
    assert TiktokCollector._to_dt("") is None
    assert TiktokCollector._to_dt(None) is None
    assert TiktokCollector._to_dt(0) is None
    assert TiktokCollector._to_dt("nope") is None


# ── _upsert_profile / _upsert_post (DB-shaped) ────────────────────────────


@pytest.mark.asyncio
async def test_upsert_profile_returns_uuid(monkeypatch):
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    pool = _make_pool(fetchrow_returns={"id": "uuid-abc"})
    c.pool = pool
    author = {
        "id": "12345",
        "uniqueId": "bryan",
        "nickname": "Bryan",
        "signature": "bio",
        "verified": True,
        "privateAccount": False,
        "avatarLarger": "https://x/avatar.jpg",
    }
    stats = {"followingCount": 1, "followerCount": 2, "heartCount": 3,
             "videoCount": 4, "diggCount": 5}
    out = await c._upsert_profile(author, stats)
    assert out == "uuid-abc"
    assert pool._conn.fetchrow.await_count == 2
    fetch_sql = [call.args[0] for call in pool._conn.fetchrow.await_args_list]
    assert "FROM tiktok_profiles" in fetch_sql[0]
    assert "INSERT INTO tiktok_profiles" in fetch_sql[1]


@pytest.mark.asyncio
async def test_upsert_profile_returns_none_without_id():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c.pool = _make_pool()
    out = await c._upsert_profile({"uniqueId": "x"})  # no id
    assert out is None


@pytest.mark.asyncio
async def test_upsert_profile_returns_none_for_non_dict():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c.pool = _make_pool()
    assert await c._upsert_profile("not-a-dict") is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_upsert_post_skips_without_id():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c.pool = _make_pool()
    await c._upsert_post({}, "bryan", "uuid")
    c.pool._conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_post_executes_with_full_payload():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c.pool = _make_pool()
    data = {
        "id": "999",
        "desc": "hello world",
        "video": {"downloadAddr": "https://x/v.mp4", "cover": "https://x/c.jpg", "duration": 30},
        "stats": {"playCount": 10, "diggCount": 1, "commentCount": 2, "shareCount": 3},
        "music": {"id": 555, "title": "song", "authorName": "artist", "duration": 60},
        "textExtra": [
            {"hashtagName": "viral"}, {"userUniqueId": "alice"},
        ],
        "challenges": [{"title": "chal1"}],
        "createTime": 1700000000,
        "duetEnabled": True,
        "stitchEnabled": False,
    }
    await c._upsert_post(data, "bryan", "uuid-abc")
    c.pool._conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_post_looks_up_profile_by_username_when_uuid_missing():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c.pool = _make_pool(fetchrow_returns={"id": "found-uuid"})
    await c._upsert_post({"id": "1"}, "bryan", profile_uuid=None)
    # fetchrow ran (the profile lookup) and execute ran (the upsert).
    c.pool._conn.fetchrow.assert_awaited_once()
    c.pool._conn.execute.assert_awaited_once()


# ── collect_user_profile / collect_user_videos ────────────────────────────


@pytest.mark.asyncio
async def test_collect_user_profile_invalid_username_returns_none():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    out = await c.collect_user_profile("bad name!")
    assert out is None


@pytest.mark.asyncio
async def test_collect_user_profile_happy_path(monkeypatch):
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c.pool = _make_pool(fetchrow_returns={"id": "profile-uuid"})
    monkeypatch.setattr(c, "_scrape_profile_metadata", AsyncMock(return_value={"status": "ok"}))
    monkeypatch.setattr(c, "_record_profile_access", AsyncMock())
    monkeypatch.setattr(c, "wait_rate_limit", AsyncMock())
    # Skip quota gating.
    c._quota = None

    out = await c.collect_user_profile("bryan")
    assert out == "profile-uuid"
    c._scrape_profile_metadata.assert_awaited_once_with("bryan")
    c._record_profile_access.assert_awaited_once_with("bryan", True)


@pytest.mark.asyncio
async def test_collect_user_records_api_fallback_success(monkeypatch):
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c._quota = None
    c._browser_fallback = False
    monkeypatch.setattr(c, "_scrape_profile_metadata", AsyncMock(return_value={"status": "missing"}))
    monkeypatch.setattr(c, "_collect_via_api", AsyncMock(return_value=True))
    monkeypatch.setattr(c, "_record_profile_access", AsyncMock())

    out = await c._collect_user("bryan")

    assert out == "collected"
    c._record_profile_access.assert_awaited_once_with("bryan", True)


@pytest.mark.asyncio
async def test_collect_user_profile_quota_exhausted(monkeypatch):
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c.pool = _make_pool(fetchrow_returns={"id": "x"})
    monkeypatch.setattr(c, "_scrape_profile_metadata", AsyncMock())
    monkeypatch.setattr(c, "wait_rate_limit", AsyncMock())
    quota = MagicMock()
    quota.has_quota = AsyncMock(return_value=False)
    c._quota = quota

    out = await c.collect_user_profile("bryan")
    assert out is None
    c._scrape_profile_metadata.assert_not_awaited()


def test_extract_profile_from_universal_state():
    state = {
        "__DEFAULT_SCOPE__": {
            "webapp.user-detail": {
                "userInfo": {
                    "user": {
                        "id": "123",
                        "uniqueId": "bryan",
                        "nickname": "Bryan",
                        "privateAccount": False,
                    },
                    "stats": {"followerCount": 42},
                }
            }
        }
    }
    html = (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        + tiktok_mod.json.dumps(state)
        + "</script>"
    )
    author, stats = TiktokCollector._extract_profile_from_html(html, "bryan")
    assert author["id"] == "123"
    assert stats["followerCount"] == 42


@pytest.mark.asyncio
async def test_collect_user_profile_only_when_over_follower_cap(monkeypatch):
    monkeypatch.setenv("TIKTOK_VIDEO_FOLLOWER_CAP", "300")
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c._quota = None
    c._browser_fallback = False
    c._use_gallery_dl = True
    c._use_yt_dlp = True
    monkeypatch.setattr(c, "_stored_followers_count", AsyncMock(return_value=None))
    monkeypatch.setattr(
        c,
        "_scrape_profile_metadata",
        AsyncMock(return_value={"status": "ok", "followers_count": 301, "is_private": False}),
    )
    monkeypatch.setattr(c, "_collect_via_gallery_dl", AsyncMock())
    monkeypatch.setattr(c, "_collect_via_yt_dlp", AsyncMock())
    monkeypatch.setattr(c, "_collect_via_api", AsyncMock())
    monkeypatch.setattr(c, "_record_profile_access", AsyncMock())

    out = await c._collect_user("bryan")

    assert out == "profile_only"
    c._collect_via_gallery_dl.assert_not_awaited()
    c._collect_via_yt_dlp.assert_not_awaited()
    c._collect_via_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_user_skips_ytdlp_after_clean_empty_gallery(monkeypatch):
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c._quota = None
    c._browser_fallback = False
    c._use_gallery_dl = True
    c._use_yt_dlp = True
    monkeypatch.setattr(c, "_stored_followers_count", AsyncMock(return_value=1))
    monkeypatch.setattr(
        c,
        "_scrape_profile_metadata",
        AsyncMock(return_value={"status": "ok", "followers_count": 1, "is_private": False}),
    )
    async def _empty_gallery(_username, _profile_url):
        c._last_gallery_dl_empty_user = "bryan"
        return False

    monkeypatch.setattr(c, "_collect_via_gallery_dl", AsyncMock(side_effect=_empty_gallery))
    monkeypatch.setattr(c, "_collect_via_yt_dlp", AsyncMock(return_value=True))
    monkeypatch.setattr(c, "_collect_via_api", AsyncMock(return_value=False))
    monkeypatch.setattr(c, "_record_profile_access", AsyncMock())

    out = await c._collect_user("bryan")

    assert out == "empty"
    c._collect_via_yt_dlp.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_user_respects_profile_backoff(monkeypatch):
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c._quota = None
    c._browser_fallback = False
    c._use_gallery_dl = True
    c._use_yt_dlp = True
    monkeypatch.setattr(c, "_stored_followers_count", AsyncMock(return_value=1))
    monkeypatch.setattr(
        c,
        "_scrape_profile_metadata",
        AsyncMock(return_value={"status": "ok", "followers_count": 1, "is_private": False}),
    )
    monkeypatch.setattr(c, "_profile_backoff_remaining", MagicMock(return_value=123))
    monkeypatch.setattr(c, "_collect_via_gallery_dl", AsyncMock(return_value=True))
    monkeypatch.setattr(c, "_collect_via_yt_dlp", AsyncMock(return_value=True))
    monkeypatch.setattr(c, "_collect_via_api", AsyncMock(return_value=True))

    out = await c._collect_user("bryan")

    assert out == "delayed"
    c._collect_via_gallery_dl.assert_not_awaited()
    c._collect_via_yt_dlp.assert_not_awaited()
    c._collect_via_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_user_skips_ytdlp_after_zero_file_gallery_timeout(monkeypatch):
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c._quota = None
    c._browser_fallback = False
    c._use_gallery_dl = True
    c._use_yt_dlp = True
    monkeypatch.setattr(c, "_stored_followers_count", AsyncMock(return_value=1))
    monkeypatch.setattr(
        c,
        "_scrape_profile_metadata",
        AsyncMock(return_value={"status": "ok", "followers_count": 1, "is_private": False}),
    )
    monkeypatch.setattr(c, "_profile_backoff_remaining", MagicMock(return_value=0))

    async def _timeout_gallery(_username, _profile_url):
        c._last_gallery_dl_timeout_user = "bryan"
        return False

    monkeypatch.setattr(c, "_collect_via_gallery_dl", AsyncMock(side_effect=_timeout_gallery))
    monkeypatch.setattr(c, "_collect_via_yt_dlp", AsyncMock(return_value=True))
    monkeypatch.setattr(c, "_collect_via_api", AsyncMock(return_value=False))
    monkeypatch.setattr(c, "_record_profile_access", AsyncMock())

    out = await c._collect_user("bryan")

    assert out == "delayed"
    c._collect_via_yt_dlp.assert_not_awaited()
    c._collect_via_api.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_user_videos_invalid_returns_zero():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    assert await c.collect_user_videos("bad name!") == 0


@pytest.mark.asyncio
async def test_collect_user_videos_returns_known_id_delta(monkeypatch):
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c._quota = None

    async def _fake_collect_user(username):
        c._known_ids.update({"id1", "id2", "id3"})

    monkeypatch.setattr(c, "_collect_user", _fake_collect_user)
    out = await c.collect_user_videos("bryan")
    assert out == 3


@pytest.mark.asyncio
async def test_collect_user_videos_quota_exhausted(monkeypatch):
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    quota = MagicMock()
    quota.has_quota = AsyncMock(return_value=False)
    quota.consume = AsyncMock()
    c._quota = quota
    monkeypatch.setattr(c, "_collect_user", AsyncMock())

    out = await c.collect_user_videos("bryan")
    assert out == 0
    c._collect_user.assert_not_awaited()


# ── collect_following / TiktokEdgeFetcher ─────────────────────────────────


@pytest.mark.asyncio
async def test_collect_following_yields_unique_usernames(monkeypatch):
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    monkeypatch.setattr(c, "wait_rate_limit", AsyncMock())

    html = '"uniqueId":"alice","uniqueId":"bob","uniqueId":"alice","uniqueId":"bryan"'
    resp = MagicMock(status_code=200, text=html)
    resp.raise_for_status = MagicMock()
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=resp)

    @asynccontextmanager
    async def _client_cm(*a, **kw):
        yield fake_client

    monkeypatch.setattr(tiktok_mod.httpx, "AsyncClient", _client_cm)

    out = []
    async for u in c.collect_following("bryan"):
        out.append(u)
    # 'bryan' (self) is filtered, dupes deduped.
    assert out == ["alice", "bob"]


@pytest.mark.asyncio
async def test_collect_following_invalid_username_yields_nothing():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    out = [u async for u in c.collect_following("bad name!")]
    assert out == []


@pytest.mark.asyncio
async def test_collect_following_swallows_http_failure(monkeypatch):
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    monkeypatch.setattr(c, "wait_rate_limit", AsyncMock())

    fake_client = MagicMock()
    fake_client.get = AsyncMock(side_effect=RuntimeError("net"))

    @asynccontextmanager
    async def _client_cm(*a, **kw):
        yield fake_client

    monkeypatch.setattr(tiktok_mod.httpx, "AsyncClient", _client_cm)

    out = [u async for u in c.collect_following("bryan")]
    assert out == []


def test_make_edge_fetcher_returns_fetcher():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    f = c.make_edge_fetcher()
    assert isinstance(f, TiktokEdgeFetcher)
    assert f._c is c


# ── spider_related_creators ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spider_related_creators_no_pool_returns_zero():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c.pool = None
    out = await c.spider_related_creators("bryan", max_hops=1)
    assert out == 0


@pytest.mark.asyncio
async def test_spider_related_creators_invalid_seed_returns_zero():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c.pool = _make_pool()
    out = await c.spider_related_creators("bad name!", max_hops=1)
    assert out == 0


@pytest.mark.asyncio
async def test_spider_related_creators_runs_spider(monkeypatch):
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c.pool = _make_pool()

    fake_spider = MagicMock()
    fake_spider.run = AsyncMock(return_value=7)
    monkeypatch.setattr(c, "make_spider_discover", lambda **kw: fake_spider)

    out = await c.spider_related_creators("bryan", max_hops=2)
    assert out == 7
    fake_spider.run.assert_awaited_once_with(seeds=["bryan"])


# ── _load_tracker_state ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_tracker_state_pulls_known_ids():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c.pool = _make_pool(fetch_returns=[
        {"platform_post_id": "p1"},
        {"platform_post_id": "p2"},
    ])
    await c._load_tracker_state()
    assert c._tracked_ids == {"p1", "p2"}


@pytest.mark.asyncio
async def test_load_tracker_state_no_pool_is_noop():
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c.pool = None
    await c._load_tracker_state()
    assert c._tracked_ids == set()


# ── download_media ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_media_writes_vault_blob(monkeypatch, tmp_path):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(tiktok_mod, "VAULT_ROOT", vault_root)
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c.insert_media_item = AsyncMock(return_value=True)
    c.send_to_dlq = AsyncMock()

    data = b"tiktok video bytes"
    digest = hashlib.sha256(data).hexdigest()

    await c.download_media({
        "entity_id": "alice",
        "entity_name": "alice",
        "content_type": "video",
        "content_id": "7000000000000000001",
        "extension": "mp4",
        "data": data,
        "raw": {"video_id": "7000000000000000001"},
    })

    kwargs = c.insert_media_item.await_args.kwargs
    stored_path = Path(kwargs["file_path"])
    assert stored_path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.mp4"
    assert stored_path.read_bytes() == data
    assert kwargs["sha256"] == digest
    assert kwargs["file_size"] == len(data)
    assert kwargs["source_url"] == "https://www.tiktok.com/@alice/video/7000000000000000001"
    assert kwargs["metadata"]["raw"] == {"video_id": "7000000000000000001"}
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    assert kwargs["metadata"]["vault_artifact"]["partial"] is False
    assert kwargs["metadata"]["vault_artifact"]["blob_path"].startswith("media/blobs/")
    c.send_to_dlq.assert_not_awaited()


# ── _collect_via_playwright (browser fallback) ────────────────────────────


@pytest.mark.asyncio
async def test_collect_via_playwright_disabled_when_browser_fallback_off(monkeypatch):
    """Instance-level toggle off → fallback returns False without doing work."""
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c._browser_fallback = False
    assert await c._collect_via_playwright("bryan") is False


@pytest.mark.asyncio
async def test_browser_fallback_triggered_on_gallery_dl_failure(monkeypatch, tmp_path):
    """When gallery-dl fails and the env flag is on, the browser fallback runs."""
    monkeypatch.setenv("TIKTOK_BROWSER_FALLBACK_ENABLED", "true")
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c._browser_fallback = True
    c._cookies_file = str(tmp_path / "cookies.txt")
    # Make download_media a no-op so we don't touch the filesystem / DB.
    c.download_media = AsyncMock(return_value=None)
    c.is_known = lambda _id: False  # type: ignore[assignment]

    browser_tmp = tmp_path / "browser_tmp"
    monkeypatch.setenv("TIKTOK_BROWSER_TEMP_DIR", str(browser_tmp))
    fp = browser_tmp / "v.mp4"
    fp.parent.mkdir(parents=True)
    fp.write_bytes(b"x" * 100)

    calls: dict[str, list] = {"init": [], "download_user": [], "close": []}

    class _FakeBrowser:
        def __init__(self, **kw):
            calls["init"].append(kw)

        async def download_user(self, username, max_videos=50):
            calls["download_user"].append((username, max_videos))
            return [
                {"video_id": "v1", "file_path": str(fp), "metadata": {"u": username}},
            ]

        async def close(self):
            calls["close"].append(True)

    # Replace the lazy import target. The collector imports
    # ``from src.core.tiktok_browser import TikTokBrowserDownloader`` inside
    # the method, so patching that module-level attribute is sufficient.
    import src.core.tiktok_browser as tb_mod
    monkeypatch.setattr(tb_mod, "TikTokBrowserDownloader", _FakeBrowser)

    ok = await c._collect_via_playwright("alice")
    assert ok is True
    assert calls["download_user"] == [("alice", 60)]
    assert calls["close"] == [True]
    c.download_media.assert_awaited()  # at least one ingest happened
    assert fp.exists() is False
    assert "output_dir" not in calls["init"][0]


@pytest.mark.asyncio
async def test_browser_fallback_disabled_by_env(monkeypatch):
    """Env flag false → browser fallback never instantiated."""
    monkeypatch.setenv("TIKTOK_BROWSER_FALLBACK_ENABLED", "false")
    with patch.object(TiktokCollector, "_check_tool", staticmethod(lambda *_: False)):
        c = TiktokCollector()
    c._browser_fallback = True  # instance flag intentionally on

    instantiated: list = []

    class _FakeBrowser:
        def __init__(self, **kw):
            instantiated.append(kw)

        async def download_user(self, *a, **kw):
            return [{"video_id": "v1", "file_path": "/dev/null", "metadata": {}}]

        async def close(self):
            pass

    import src.core.tiktok_browser as tb_mod
    monkeypatch.setattr(tb_mod, "TikTokBrowserDownloader", _FakeBrowser)

    ok = await c._collect_via_playwright("bob")
    assert ok is False
    assert instantiated == []  # never built


# ── ValidationResult dataclass smoke ─────────────────────────────────────


def test_validation_result_dataclass_defaults():
    vr = ValidationResult(is_valid=True)
    assert vr.is_rate_limited is False
    assert vr.is_network_error is False
    assert vr.invalid_reason is None
    assert vr.should_retry is False
