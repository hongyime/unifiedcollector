"""Tests for src/collectors/youtube.py — Wave 2 batch.

Pure unit. yt-dlp / Google OAuth / httpx are mocked at boundaries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.base_collector import BaseCollector
from src.collectors import youtube as youtube_mod
from src.collectors.youtube import (
    LIKED_VIDEOS_PLAYLIST_ID,
    YT_API_BASE,
    YoutubeCollector,
    parse_iso8601_duration,
    _safe_log_text,
    _classify_ytdlp_media_failure,
)


# ── fixtures / helpers ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_drive(tmp_path, monkeypatch):
    """Redirect DRIVE_PATH and ensure yt-dlp probe doesn't actually run."""
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))
    # Pretend yt-dlp is installed — we never call it.
    monkeypatch.setattr(
        YoutubeCollector, "_check_yt_dlp",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        youtube_mod,
        "check_tool",
        lambda name: name in {"ffmpeg", "ffprobe"},
    )
    yield


def _make_pool() -> MagicMock:
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool._conn = conn
    return pool


def _new_collector(monkeypatch=None, **env: str) -> YoutubeCollector:
    if monkeypatch is not None:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    coll = YoutubeCollector()
    coll.set_pool(_make_pool())
    return coll


def _make_response(*, status: int = 200, json_body: Any = None,
                   text: str = "", content: bytes = b""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.content = content
    r.json = MagicMock(return_value=json_body if json_body is not None else {})
    r.raise_for_status = MagicMock()
    return r


def _patch_httpx_async_client(monkeypatch, response_or_responses):
    """Patch ``httpx.AsyncClient`` *as imported by the youtube module* so
    every ``async with httpx.AsyncClient(...) as client`` returns a stub
    whose ``.get`` produces our pre-baked response(s).
    """
    if not isinstance(response_or_responses, list):
        response_or_responses = [response_or_responses]
    queue = list(response_or_responses)

    class _StubAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *a, **kw):
            if len(queue) > 1:
                return queue.pop(0)
            return queue[0]

        async def aclose(self):
            return None

    monkeypatch.setattr(youtube_mod.httpx, "AsyncClient", _StubAsyncClient)


# ── module-level helpers ──────────────────────────────────────────────────


def test_parse_iso8601_duration_full():
    assert parse_iso8601_duration("PT1H23M45S") == 1 * 3600 + 23 * 60 + 45


def test_parse_iso8601_duration_minutes_only():
    assert parse_iso8601_duration("PT5M") == 300


def test_parse_iso8601_duration_seconds_only():
    assert parse_iso8601_duration("PT42S") == 42


def test_safe_log_text_redacts_url_query_secrets():
    msg = (
        "Client error for url "
        "'https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&key=AIzaSecret&token=abc'"
    )

    redacted = _safe_log_text(msg)

    assert "AIzaSecret" not in redacted
    assert "token=abc" not in redacted
    assert "key=<redacted>" in redacted


def test_safe_log_text_names_blank_exceptions():
    assert _safe_log_text(TimeoutError()) == "TimeoutError"
    assert _safe_log_text(AssertionError()) == "AssertionError"


def test_parse_iso8601_duration_empty_or_garbage():
    assert parse_iso8601_duration("") == 0
    assert parse_iso8601_duration(None) == 0  # type: ignore[arg-type]
    # No PT prefix → regex finds nothing matchable → 0
    assert parse_iso8601_duration("garbage") == 0


def test_extract_youtube_refs_dedupes_handles_and_channel_urls():
    refs = YoutubeCollector._extract_youtube_refs(
        "See @friend, https://youtube.com/@friend and "
        "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv"
    )

    assert refs == [
        {"key_type": "channel", "platform_channel_id": "UCabcdefghijklmnopqrstuv", "profile_key": "UCabcdefghijklmnopqrstuv"},
        {"key_type": "handle", "handle": "friend", "profile_key": "@friend"},
    ]


def test_module_constants():
    assert LIKED_VIDEOS_PLAYLIST_ID == "LL"
    assert YT_API_BASE.startswith("https://")


# ── construction ─────────────────────────────────────────────────────────


def _clear_youtube_env(monkeypatch):
    """Strip any YT/Google API vars so constructor sees a clean env."""
    for key in (
        "YOUTUBE_API_KEY",
        "GOOGLE_API_KEY",
        "YOUTUBE_OAUTH_CREDENTIALS",
        "YOUTUBE_OAUTH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_constructor_defaults(monkeypatch):
    _clear_youtube_env(monkeypatch)
    coll = YoutubeCollector()
    assert coll.SOURCE_NAME == "youtube"
    assert coll._api_key == ""
    assert coll._oauth_credentials is None
    assert coll._download_videos is True


def test_constructor_reads_env(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIza_fake_key_12345678")
    monkeypatch.setenv("YOUTUBE_DOWNLOAD_VIDEOS", "false")
    monkeypatch.setenv("YOUTUBE_FETCH_TRANSCRIPTS", "false")
    coll = YoutubeCollector()
    assert coll._api_key.startswith("AIza_")
    assert coll._download_videos is False
    assert coll._fetch_transcripts is False


def test_account_media_dir_isolated_by_api_key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaSyXX_yyyy")
    coll = _new_collector(monkeypatch)
    p = coll.account_media_dir
    assert p.exists()
    assert p.name.startswith("api_")
    assert p.parent.name == "youtube"


def test_account_media_dir_oauth_when_no_api_key(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    coll = _new_collector(monkeypatch)
    p = coll.account_media_dir
    assert p.name == "api_oauth"


# ── _yt_auth ─────────────────────────────────────────────────────────────


def test_yt_auth_prefers_oauth_when_present():
    coll = YoutubeCollector()
    coll._oauth_credentials = "tok-abc"
    headers, params = coll._yt_auth({"part": "snippet"})
    assert headers["Authorization"] == "Bearer tok-abc"
    assert "key" not in params
    assert params["part"] == "snippet"


def test_yt_auth_falls_back_to_api_key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaXX")
    coll = YoutubeCollector()
    headers, params = coll._yt_auth(None)
    assert headers == {}
    assert params["key"] == "AIzaXX"


def test_yt_auth_no_creds(monkeypatch):
    _clear_youtube_env(monkeypatch)
    coll = YoutubeCollector()
    headers, params = coll._yt_auth({})
    assert headers == {}
    assert "key" not in params


# ── _vtt_to_text (static helper) ─────────────────────────────────────────


def test_vtt_to_text_strips_headers_and_dedupes():
    vtt = (
        "WEBVTT\n"
        "Kind: captions\n"
        "Language: en\n"
        "\n"
        "1\n"
        "00:00:01.000 --> 00:00:03.000\n"
        "Hello world\n"
        "\n"
        "2\n"
        "00:00:03.000 --> 00:00:05.000\n"
        "Hello world\n"  # dup → dropped
        "\n"
        "3\n"
        "00:00:05.000 --> 00:00:07.000\n"
        "<00:00:05.500><c>tagged</c> line\n"
    )
    out = YoutubeCollector._vtt_to_text(vtt)
    lines = out.splitlines()
    assert lines == ["Hello world", "tagged line"]


# ── _parse_relative_timestamp ────────────────────────────────────────────


def test_parse_relative_timestamp_returns_datetime():
    out = YoutubeCollector._parse_relative_timestamp("3 days ago")
    assert out is not None
    # plus '(edited)' suffix is tolerated
    out2 = YoutubeCollector._parse_relative_timestamp("1 hour ago (edited)")
    assert out2 is not None


def test_parse_relative_timestamp_returns_none_on_garbage():
    assert YoutubeCollector._parse_relative_timestamp("") is None
    assert YoutubeCollector._parse_relative_timestamp("never") is None
    assert YoutubeCollector._parse_relative_timestamp("3 light-years ago") is None


# ── _ensure_auth ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_auth_with_api_key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaXX")
    coll = YoutubeCollector()
    assert await coll._ensure_auth() is True


@pytest.mark.asyncio
async def test_ensure_auth_with_oauth():
    coll = YoutubeCollector()
    coll._oauth_credentials = "tok-x"
    assert await coll._ensure_auth() is True


@pytest.mark.asyncio
async def test_ensure_auth_no_creds(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    coll = YoutubeCollector()
    coll._load_oauth_credentials = lambda: None  # no oauth file
    assert await coll._ensure_auth() is False


# ── _api_get ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_get_returns_json_on_200(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    _patch_httpx_async_client(
        monkeypatch, _make_response(status=200, json_body={"ok": True})
    )
    out = await coll._api_get("channels", {"id": "x"})
    assert out == {"ok": True}


@pytest.mark.asyncio
async def test_api_get_returns_empty_on_non_200(monkeypatch, caplog):
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    _patch_httpx_async_client(
        monkeypatch, _make_response(status=403, text="quotaExceeded")
    )
    with caplog.at_level("WARNING", logger="src.collectors.youtube"):
        out = await coll._api_get("channels", {"id": "x"})
    assert out == {}
    assert any("status=403" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_api_get_skips_when_persisted_api_cooldown_is_active(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    coll.pool._conn.fetchrow = AsyncMock(return_value={
        "cooldown_until": datetime.now(timezone.utc) + timedelta(seconds=90),
    })

    class _NoClient:
        def __init__(self, *args, **kwargs):  # pragma: no cover - should not be reached
            raise AssertionError("Data API should not be called during cooldown")

    monkeypatch.setattr(youtube_mod.httpx, "AsyncClient", _NoClient)

    out = await coll._api_get("channels", {"id": "UC123"})

    assert out == {}
    assert coll._youtube_api_cooldown_remaining() > 0


@pytest.mark.asyncio
async def test_record_api_request_sets_local_cooldown_on_403(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    coll._api_403_cooldown_seconds = 123

    await coll._record_api_request("channels.list", status_code=403)

    assert coll._youtube_api_cooldown_remaining() > 100
    calls = coll.pool._conn.execute.await_args_list
    assert any(
        "INSERT INTO rate_limit_events" in call.args[0]
        and call.args[5] == 123
        for call in calls
    )


# ── _upsert_channel ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_channel_no_auth_writes_minimal_row(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    coll = _new_collector(monkeypatch)
    coll._has_auth = False
    out = await coll._upsert_channel("UC123", "Some Channel")
    # Without auth we never query the API → no uploads playlist, 0 subs.
    # _upsert_channel returns (uploads_playlist_id | None, subscriber_count).
    assert out == (None, 0)
    coll.pool._conn.execute.assert_awaited_once()
    args = coll.pool._conn.execute.await_args.args
    assert args[1] == "UC123"
    assert args[2] == "Some Channel"


@pytest.mark.asyncio
async def test_upsert_channel_with_auth_extracts_uploads(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    coll._has_auth = True
    api_resp = _make_response(status=200, json_body={
        "items": [{
            "snippet": {
                "description": "d", "customUrl": "@x",
                "publishedAt": "2020-01-01T00:00:00Z",
                "thumbnails": {"high": {"url": "https://t/x.jpg"}},
            },
            "statistics": {"viewCount": "1", "subscriberCount": "2",
                           "videoCount": "3"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU123"}},
        }],
    })
    _patch_httpx_async_client(monkeypatch, api_resp)
    out = await coll._upsert_channel("UC123", "Some Channel")
    # Returns (uploads_playlist_id, subscriber_count) — subs "2" from statistics.
    assert out == ("UU123", 2)


@pytest.mark.asyncio
async def test_upsert_channel_with_auth_returns_none_when_not_found(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    coll._has_auth = True
    _patch_httpx_async_client(
        monkeypatch, _make_response(status=200, json_body={"items": []}),
    )
    out = await coll._upsert_channel("UC_missing", "x")
    assert out == (None, 0)
    # Important: no youtube_channels row when channels.list confirms not found.
    calls = coll.pool._conn.execute.await_args_list
    assert not any("INSERT INTO youtube_channels" in c.args[0] for c in calls)


@pytest.mark.asyncio
async def test_collect_channel_cooldown_does_not_confirm_missing_channel(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    coll._has_auth = True
    coll._api_cooldown_until = time.time() + 90
    coll._download_videos_via_yt_dlp = AsyncMock()
    coll._mark_channel_skip = AsyncMock()

    result = await coll._collect_channel("UC123")

    assert result["reason"] == "collected"
    coll._mark_channel_skip.assert_not_awaited()
    coll._download_videos_via_yt_dlp.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_channel_skips_fallback_when_uploads_playlist_is_empty(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    coll._has_auth = True
    coll._use_yt_dlp = True
    coll._download_videos = True
    coll._resolve_channel = AsyncMock(return_value=("UC_empty", "Empty Channel"))
    coll._upsert_channel = AsyncMock(return_value=("UUempty", 0))

    async def _empty_uploads(*args, **kwargs):
        coll._skip_channel_fallback_after_api_empty.add("UC_empty")
        return []

    coll._collect_video_list_via_api = AsyncMock(side_effect=_empty_uploads)
    coll._download_videos_via_yt_dlp = AsyncMock()

    result = await coll._collect_channel("UC_empty")

    assert result["reason"] == "collected"
    assert result["video_ids"] == 0
    coll._download_videos_via_yt_dlp.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_video_list_404_marks_channel_fallback_skip(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    coll._has_auth = True
    coll._record_api_request = AsyncMock()
    coll._mark_channel_skip = AsyncMock()
    _patch_httpx_async_client(monkeypatch, _make_response(status=404))

    out = await coll._collect_video_list_via_api("UC404", "Gone Channel", "UU404")

    assert out == []
    assert "UC404" in coll._skip_channel_fallback_after_api_empty
    coll._mark_channel_skip.assert_awaited_once_with(
        "UC404",
        "uploads_playlist_404",
        {"playlist_id": "UU404"},
    )


# ── _upsert_video ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_video_dict_id_shape(monkeypatch):
    coll = _new_collector(monkeypatch)
    # First call (fetchrow) returns the channel uuid
    coll.pool._conn.fetchrow = AsyncMock(return_value={"id": "uuid-1"})

    video = {
        "id": {"kind": "youtube#video", "videoId": "VID123"},
        "snippet": {"title": "T", "description": "D",
                    "publishedAt": "2024-01-01T00:00:00Z"},
        "statistics": {"viewCount": "10", "likeCount": "1"},
    }
    await coll._upsert_video("UC123", video)
    coll.pool._conn.execute.assert_awaited_once()
    args = coll.pool._conn.execute.await_args.args
    assert args[1] == "VID123"
    assert args[2] == "uuid-1"
    assert "collected_at = NOW()" in args[0]


@pytest.mark.asyncio
async def test_upsert_video_skips_when_no_id(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll.pool._conn.fetchrow = AsyncMock(return_value={"id": "uuid-1"})
    await coll._upsert_video("UC123", {"id": None})
    coll.pool._conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_video_extracts_description_links(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll.pool._conn.fetchrow = AsyncMock(return_value={"id": "uuid-1"})

    video = {
        "id": "VID999",
        "snippet": {
            "title": "Links",
            "description": "More at https://example.com/a and https://x.com/b.",
            "publishedAt": "2024-01-01T00:00:00Z",
        },
        "statistics": {},
    }

    await coll._upsert_video("UC123", video)

    calls = coll.pool._conn.execute.await_args_list
    assert any("INSERT INTO youtube_videos" in c.args[0] for c in calls)
    link_calls = [c for c in calls if "INSERT INTO discovered_links" in c.args[0]]
    assert len(link_calls) == 2
    assert {c.args[6] for c in link_calls} == {"https://example.com/a", "https://x.com/b"}
    assert all(c.args[1] == "youtube" for c in link_calls)
    assert all(c.args[3] == "VID999" for c in link_calls)


# ── _resolve_channel ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_channel_no_auth_passthrough(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll._has_auth = False
    cid, name = await coll._resolve_channel("UC_abc")
    assert (cid, name) == ("UC_abc", "UC_abc")


@pytest.mark.asyncio
async def test_resolve_channel_uc_lookup(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    coll._has_auth = True
    _patch_httpx_async_client(monkeypatch, _make_response(
        status=200,
        json_body={"items": [{"id": "UCxyz", "snippet": {"title": "Hello"}}]},
    ))
    cid, name = await coll._resolve_channel("UCxyz")
    assert cid == "UCxyz"
    assert name == "Hello"


@pytest.mark.asyncio
async def test_resolve_channel_search_when_no_prefix(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    coll._has_auth = True
    _patch_httpx_async_client(monkeypatch, _make_response(
        status=200, json_body={
            "items": [{"id": {"channelId": "UCfound"},
                       "snippet": {"title": "Found"}}],
        },
    ))
    cid, name = await coll._resolve_channel("just a query")
    assert cid == "UCfound"
    assert name == "Found"


@pytest.mark.asyncio
async def test_download_media_writes_vault_blob(monkeypatch, tmp_path):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(youtube_mod, "VAULT_ROOT", vault_root)
    coll = _new_collector(monkeypatch)
    coll.insert_media_item = AsyncMock(return_value=True)
    coll.send_to_dlq = AsyncMock()

    data = b"youtube thumbnail bytes"
    digest = hashlib.sha256(data).hexdigest()

    await coll.download_media({
        "entity_id": "UC123",
        "entity_name": "Example Channel",
        "content_type": "thumbnail",
        "content_id": "abc123xyz90",
        "extension": "jpg",
        "data": data,
        "raw": {"video_id": "abc123xyz90"},
    })

    kwargs = coll.insert_media_item.await_args.kwargs
    stored_path = Path(kwargs["file_path"])
    assert stored_path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    assert stored_path.read_bytes() == data
    assert kwargs["sha256"] == digest
    assert kwargs["file_size"] == len(data)
    assert kwargs["source_url"] == "https://www.youtube.com/watch?v=abc123xyz90"
    assert kwargs["metadata"]["raw"] == {"video_id": "abc123xyz90"}
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    assert kwargs["metadata"]["vault_artifact"]["partial"] is False
    assert kwargs["metadata"]["vault_artifact"]["blob_path"].startswith("media/blobs/")
    coll.send_to_dlq.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_media_streams_local_source_path(monkeypatch, tmp_path):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(youtube_mod, "VAULT_ROOT", vault_root)
    coll = _new_collector(monkeypatch)
    coll.insert_media_item = AsyncMock(return_value=True)
    coll.send_to_dlq = AsyncMock()

    def _fail_byte_writer(**_kwargs):
        raise AssertionError("source_path should use write_atomic_artifact_from_path")

    monkeypatch.setattr(youtube_mod, "write_atomic_artifact", _fail_byte_writer)

    source = tmp_path / "downloaded.mp4"
    data = b"youtube video bytes"
    source.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()

    inserted = await coll.download_media({
        "entity_id": "UC123",
        "entity_name": "Example Channel",
        "content_type": "video",
        "content_id": "video_abc123xyz90",
        "extension": "mp4",
        "source_path": str(source),
        "source_url": "https://www.youtube.com/watch?v=abc123xyz90",
    })

    assert inserted is True
    assert not source.exists()
    kwargs = coll.insert_media_item.await_args.kwargs
    stored_path = Path(kwargs["file_path"])
    assert stored_path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.mp4"
    assert stored_path.read_bytes() == data
    assert kwargs["sha256"] == digest
    assert kwargs["file_size"] == len(data)
    assert kwargs["source_url"] == "https://www.youtube.com/watch?v=abc123xyz90"
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    coll.send_to_dlq.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_media_falls_back_to_mq_thumbnail(monkeypatch, tmp_path):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(youtube_mod, "VAULT_ROOT", vault_root)
    coll = _new_collector(monkeypatch)
    coll.insert_media_item = AsyncMock(return_value=True)
    coll.send_to_dlq = AsyncMock()
    calls = []
    data = b"mq thumbnail bytes"

    class _StubAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, *a, **kw):
            calls.append(url)
            if "mqdefault" in url:
                return _make_response(status=200, content=data)
            return _make_response(status=404)

    monkeypatch.setattr(youtube_mod.httpx, "AsyncClient", _StubAsyncClient)

    inserted = await coll.download_media({
        "entity_id": "UC123",
        "entity_name": "Example Channel",
        "content_type": "thumbnail",
        "content_id": "abc123xyz90",
        "extension": "jpg",
        "url": "https://i.ytimg.com/vi/abc123xyz90/maxresdefault.jpg",
    })

    assert inserted is True
    assert calls == [
        "https://i.ytimg.com/vi/abc123xyz90/maxresdefault.jpg",
        "https://i.ytimg.com/vi/abc123xyz90/hqdefault.jpg",
        "https://i.ytimg.com/vi/abc123xyz90/mqdefault.jpg",
    ]
    assert coll.insert_media_item.await_args.kwargs["metadata"]["vault_artifact"]["ok"] is True


@pytest.mark.asyncio
async def test_download_media_thumbnail_404_is_audited_warning(monkeypatch, tmp_path, caplog):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(youtube_mod, "VAULT_ROOT", vault_root)
    coll = _new_collector(monkeypatch)
    coll.insert_media_item = AsyncMock(return_value=True)
    coll.send_to_dlq = AsyncMock()

    class _StubAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, *a, **kw):
            return youtube_mod.httpx.Response(
                404,
                request=youtube_mod.httpx.Request("GET", url),
            )

    monkeypatch.setattr(youtube_mod.httpx, "AsyncClient", _StubAsyncClient)

    coll._mark_video_media_attempt = AsyncMock()

    with caplog.at_level("INFO", logger="src.collectors.youtube"):
        inserted = await coll.download_media({
            "entity_id": "UC123",
            "entity_name": "Example Channel",
            "content_type": "thumbnail",
            "content_id": "abc123xyz90",
            "extension": "jpg",
            "url": "https://i.ytimg.com/vi/abc123xyz90/default.jpg",
        })

    assert inserted is False
    coll.insert_media_item.assert_not_awaited()
    coll.send_to_dlq.assert_not_awaited()
    coll._mark_video_media_attempt.assert_awaited_once()
    assert coll._mark_video_media_attempt.await_args.args[0] == "abc123xyz90"
    assert coll._mark_video_media_attempt.await_args.kwargs["status"] == "thumbnail_unavailable"
    assert "YouTube thumbnail unavailable abc123xyz90" in caplog.text
    assert "Download failed abc123xyz90" not in caplog.text
    assert not any(
        record.levelname == "WARNING"
        and "YouTube thumbnail unavailable abc123xyz90" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_download_media_preserves_community_source_url(monkeypatch, tmp_path):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(youtube_mod, "VAULT_ROOT", vault_root)
    coll = _new_collector(monkeypatch)
    coll.insert_media_item = AsyncMock(return_value=True)
    coll.send_to_dlq = AsyncMock()

    inserted = await coll.download_media({
        "entity_id": "UC123",
        "entity_name": "Example Channel",
        "content_type": "community_image",
        "content_id": "community_post1",
        "extension": "jpg",
        "source_url": "https://www.youtube.com/channel/UC123/community",
        "data": b"community bytes",
    })

    assert inserted is True
    assert coll.insert_media_item.await_args.kwargs["source_url"] == "https://www.youtube.com/channel/UC123/community"


@pytest.mark.asyncio
async def test_process_profile_queue_resolves_handles_and_queues_channel(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    coll._has_auth = True
    coll.pool._conn.fetch = AsyncMock(return_value=[{
        "profile_key": "@friend",
        "key_type": "handle",
        "platform_channel_id": None,
        "handle": "friend",
        "source": "mention",
        "priority": 1,
        "evidence_count": 3,
        "discovered_from": "youtube_comments:c1",
        "attempts": 1,
        "metadata": {},
    }])
    coll._resolve_channel = AsyncMock(return_value=("UCresolved12345678901234", "Friend Channel"))

    resolved = await coll._process_profile_queue(limit=1)

    assert resolved == 1
    calls = coll.pool._conn.execute.await_args_list
    assert any("UPDATE youtube_profile_queue" in c.args[0] and "resolved_at" in c.args[0] for c in calls)
    assert any("INSERT INTO youtube_spider_queue" in c.args[0] for c in calls)


@pytest.mark.asyncio
async def test_fetch_comments_records_author_edges_and_mentions(monkeypatch, tmp_path):
    coll = _new_collector(monkeypatch)
    coll.pool._conn.fetchrow = AsyncMock(return_value={"platform_channel_id": "UCowner1234567890123456"})

    def _fake_run(cmd, *args, **kwargs):
        output_template = Path(cmd[cmd.index("-o") + 1])
        out_dir = output_template.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "VID123.info.json").write_text(
            json.dumps({
                "comments": [{
                    "id": "comment1",
                    "author": "Alice",
                    "author_id": "UCauthor123456789012345",
                    "author_thumbnail": "https://example.test/a.jpg",
                    "text": "nice @friend https://www.youtube.com/channel/UCtarget123456789012345",
                    "like_count": 2,
                    "parent": "root",
                    "timestamp": 1_700_000_000,
                }]
            }),
            encoding="utf-8",
        )
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""
        return proc

    monkeypatch.setattr(youtube_mod.subprocess, "run", _fake_run)

    await coll._fetch_comments("video-uuid", "VID123")

    calls = coll.pool._conn.execute.await_args_list
    assert any("INSERT INTO youtube_comments" in c.args[0] for c in calls)
    edge_types = [c.args[7] for c in calls if "INSERT INTO youtube_edges" in c.args[0]]
    assert "commented_on_video" in edge_types
    assert "mentioned" in edge_types
    assert any("INSERT INTO youtube_profile_queue" in c.args[0] for c in calls)


# ── collect() error path ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_swallows_per_target_exceptions(monkeypatch, caplog):
    monkeypatch.setenv("YOUTUBE_SPIDER_ENABLED", "false")
    monkeypatch.setenv("YOUTUBE_FETCH_TRANSCRIPTS", "false")
    monkeypatch.setenv("YOUTUBE_FETCH_COMMENTS", "false")
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    coll._collect_channel = AsyncMock(side_effect=RuntimeError("boom"))
    coll.send_to_dlq = AsyncMock()
    coll.checkpoint.save_progress = AsyncMock()  # called only on success path

    with caplog.at_level("ERROR", logger="src.collectors.youtube"):
        await coll.collect(["UC1"])

    coll.send_to_dlq.assert_awaited_once()
    assert any("Failed youtube/UC1" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_collect_runs_targets_before_enrichment_and_discovery(monkeypatch):
    monkeypatch.setenv("YOUTUBE_COMMUNITY_ENABLED", "true")
    monkeypatch.setenv("YOUTUBE_SPIDER_ENABLED", "false")
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaK")
    coll._use_yt_dlp = True
    coll._fetch_transcripts = True
    coll._fetch_comments_enabled = False
    coll._enrich_batch_limit = 3
    events = []

    async def _collect_channel(target):
        events.append(("target", target))

    async def _enrich_transcripts_and_comments(*, limit):
        events.append(("enrich", str(limit)))

    async def _community_pass(*, batch_size):
        events.append(("community", str(batch_size)))

    coll._collect_channel = _collect_channel
    coll._enrich_transcripts_and_comments = _enrich_transcripts_and_comments
    coll._community_pass = _community_pass
    coll.checkpoint.save_progress = AsyncMock()

    await coll.collect(["UC1"])

    assert events == [("target", "UC1"), ("enrich", "3"), ("community", "15")]


# ── collect_subscriptions ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_subscriptions_requires_oauth(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaK")
    coll = _new_collector(monkeypatch)
    coll._oauth_credentials = None
    coll._has_auth = True
    out = await coll.collect_subscriptions()
    assert out == []


@pytest.mark.asyncio
async def test_collect_subscriptions_writes_cache(monkeypatch, tmp_path):
    cache = tmp_path / "subs.json"
    monkeypatch.setenv("YOUTUBE_SUBSCRIPTION_CACHE", str(cache))
    coll = _new_collector(monkeypatch)
    coll._oauth_credentials = "tok-x"
    coll._has_auth = True

    api_resp = _make_response(status=200, json_body={
        "items": [
            {"snippet": {"title": "Ch1",
                         "resourceId": {"channelId": "UC_a"}}},
            {"snippet": {"title": "Ch2",
                         "resourceId": {"channelId": "UC_b"}}},
        ],
    })
    _patch_httpx_async_client(monkeypatch, api_resp)

    out = await coll.collect_subscriptions(max_channels=2)
    assert len(out) == 2
    assert out[0]["channel_id"] == "UC_a"
    assert cache.exists()
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert len(data["subscriptions"]) == 2


# ── collect_liked_videos ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_liked_videos_requires_oauth(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll._oauth_credentials = None
    out = await coll.collect_liked_videos()
    assert out == []


@pytest.mark.asyncio
async def test_collect_liked_videos_collects_and_upserts(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll._oauth_credentials = "tok-x"
    coll._has_auth = True
    coll._upsert_liked_batch = AsyncMock()

    page = _make_response(status=200, json_body={
        "items": [
            {"snippet": {"title": "v1",
                         "resourceId": {"videoId": "vid1"},
                         "videoOwnerChannelId": "UC_a"}},
            {"snippet": {"title": "v2",
                         "resourceId": {"videoId": "vid2"},
                         "videoOwnerChannelId": "UC_b"}},
        ],
    })
    _patch_httpx_async_client(monkeypatch, page)
    out = await coll.collect_liked_videos(max_videos=2)
    assert [v["video_id"] for v in out] == ["vid1", "vid2"]
    coll._upsert_liked_batch.assert_awaited_once()


# ── collect_target_channel ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_target_channel_extracts_uc_from_url(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll._collect_channel = AsyncMock()
    out = await coll.collect_target_channel(
        "https://www.youtube.com/channel/UCxyz/videos"
    )
    assert out == ["UCxyz"]
    coll._collect_channel.assert_awaited_once_with("UCxyz")


@pytest.mark.asyncio
async def test_collect_target_channel_extracts_handle(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll._collect_channel = AsyncMock()
    out = await coll.collect_target_channel("https://www.youtube.com/@handle")
    assert out == ["@handle"]


@pytest.mark.asyncio
async def test_collect_target_channels_skips_comments(monkeypatch, tmp_path):
    f = tmp_path / "targets.txt"
    f.write_text("# header comment\nUC_a\n\n  # inline\nUC_b\n",
                 encoding="utf-8")
    coll = _new_collector(monkeypatch)
    coll._collect_channel = AsyncMock()
    out = await coll.collect_target_channels(target_file=f)
    assert out == ["UC_a", "UC_b"]
    assert coll._collect_channel.await_count == 2


@pytest.mark.asyncio
async def test_collect_target_channels_missing_file_returns_empty(monkeypatch):
    coll = _new_collector(monkeypatch)
    out = await coll.collect_target_channels(target_file="/no/such/file.txt")
    assert out == []


# ── collect_custom_playlist ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_custom_playlist_skips_when_no_yt_dlp(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll._use_yt_dlp = False
    out = await coll.collect_custom_playlist("PLxyz")
    assert out == []


@pytest.mark.asyncio
async def test_collect_custom_playlist_parses_yt_dlp_json(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll._upsert_video = AsyncMock()

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = json.dumps({
        "uploader": "UpName", "channel_id": "UC_p",
        "entries": [
            {"id": "v1", "title": "T1"},
            {"id": "v2", "title": "T2"},
        ],
    })
    fake_proc.stderr = ""

    async def _fake_run_in_executor(_executor, fn, *args):
        return fake_proc

    loop = MagicMock()
    loop.run_in_executor = _fake_run_in_executor
    monkeypatch.setattr(youtube_mod.asyncio, "get_event_loop",
                        lambda: loop)

    out = await coll.collect_custom_playlist("PLxyz")
    assert [v["video_id"] for v in out] == ["v1", "v2"]
    assert coll._upsert_video.await_count == 2


# ── batch_download ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_download_skips_when_no_yt_dlp(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll._use_yt_dlp = False
    out = await coll.batch_download(["UC_a"])
    assert out == {"total": 0, "successful": 0, "failed": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_batch_download_groups_by_channel(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll._download_videos_via_yt_dlp = AsyncMock()
    out = await coll.batch_download([
        {"channel_id": "UC_a", "video_id": "v1"},
        {"channel_id": "UC_a", "video_id": "v2"},
        {"channel_id": "UC_b", "video_id": "v3"},
    ])
    assert out["total"] == 3
    assert coll._download_videos_via_yt_dlp.await_count == 2  # two channels
    assert out["successful"] == 3


@pytest.mark.asyncio
async def test_batch_download_photos_only_routes_to_thumbs(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll._collect_thumbnails_via_yt_dlp = AsyncMock()
    coll._download_videos_via_yt_dlp = AsyncMock()
    out = await coll.batch_download([{"channel_id": "UC_a", "video_id": "v1"}],
                                    photos_only=True)
    assert out["successful"] == 1
    coll._collect_thumbnails_via_yt_dlp.assert_awaited_once()
    coll._download_videos_via_yt_dlp.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_videos_skips_channel_fallback_when_api_videos_are_known(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll._known_ids.update({"video_v1", "video_v2"})

    from src.core import subprocess_downloader

    ytdlp = AsyncMock()
    monkeypatch.setattr(subprocess_downloader, "yt_dlp_download", ytdlp)

    await coll._download_videos_via_yt_dlp("UC_a", "Channel A", ["v1", "v2"])

    ytdlp.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_videos_uses_configured_hard_timeout(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_VIDEO_DOWNLOAD_TIMEOUT="123")
    coll._filter_video_ids_for_download = AsyncMock(return_value=(["v1"], 0))
    coll._filter_video_ids_already_archived = AsyncMock(return_value=(["v1"], 0))

    from src.core import subprocess_downloader

    ytdlp = AsyncMock(return_value=MagicMock(ok=True, cancelled=False, files=[]))
    monkeypatch.setattr(subprocess_downloader, "yt_dlp_download", ytdlp)

    await coll._download_videos_via_yt_dlp("UC_a", "Channel A", ["v1"])

    assert ytdlp.await_args.kwargs["timeout"] == 123


def test_classify_ytdlp_media_failure_expected_states():
    assert _classify_ytdlp_media_failure("ERROR: [youtube] abc: This live event will begin in a few moments.") == (
        "upcoming_live",
        "info",
        6,
    )
    assert _classify_ytdlp_media_failure("ERROR: [youtube] abc: Offline.") == (
        "live_offline",
        "info",
        24,
    )
    assert _classify_ytdlp_media_failure("ERROR: [youtube] abc: Private video.") == (
        "unavailable",
        "info",
        168,
    )
    assert _classify_ytdlp_media_failure("curl: (28) Connection timed out after 30001 milliseconds") == (
        "transient_network",
        "warning",
        2,
    )
    assert _classify_ytdlp_media_failure("unexpected extractor failure") == (
        "failed",
        "warning",
        0,
    )


@pytest.mark.asyncio
async def test_download_videos_marks_upcoming_live_without_warning(monkeypatch, caplog, tmp_path):
    coll = _new_collector(monkeypatch, YOUTUBE_DOWNLOAD_DELAY="0")
    coll._filter_video_ids_for_download = AsyncMock(return_value=(["v1"], 0))
    coll._filter_video_ids_already_archived = AsyncMock(return_value=(["v1"], 0))
    coll._mark_video_media_attempt = AsyncMock()

    from src.core import subprocess_downloader
    from src.core.subprocess_downloader import DownloadResult

    ytdlp = AsyncMock(return_value=DownloadResult(
        returncode=1,
        stdout="",
        stderr="ERROR: [youtube] v1: This live event will begin in a few moments.",
        files=[],
        tempdir=tmp_path,
        elapsed=1.0,
    ))
    monkeypatch.setattr(subprocess_downloader, "yt_dlp_download", ytdlp)

    with caplog.at_level("INFO", logger="src.collectors.youtube"):
        await coll._download_videos_via_yt_dlp("UC_a", "Channel A", ["v1"])

    coll._mark_video_media_attempt.assert_awaited_once()
    assert coll._mark_video_media_attempt.await_args.kwargs["status"] == "upcoming_live"
    assert any(
        record.levelname == "INFO"
        and "youtube yt-dlp video download upcoming_live" in record.message
        for record in caplog.records
    )
    assert not any(
        record.levelname == "WARNING"
        and "youtube yt-dlp video download" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_download_videos_uses_bounded_parallel_workers(monkeypatch):
    coll = _new_collector(
        monkeypatch,
        YOUTUBE_DOWNLOAD_DELAY="0",
        YOUTUBE_MAX_CONCURRENT_DOWNLOADS="2",
    )
    coll._filter_video_ids_for_download = AsyncMock(return_value=(["v1", "v2", "v3"], 0))
    coll._filter_video_ids_already_archived = AsyncMock(return_value=(["v1", "v2", "v3"], 0))

    from src.core import subprocess_downloader

    active = 0
    max_active = 0
    calls = 0

    async def ytdlp(*args, **kwargs):
        nonlocal active, max_active, calls
        calls += 1
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return MagicMock(ok=True, cancelled=False, files=[])

    monkeypatch.setattr(subprocess_downloader, "yt_dlp_download", ytdlp)

    await coll._download_videos_via_yt_dlp("UC_a", "Channel A", ["v1", "v2", "v3"])

    assert calls == 3
    assert max_active == 2


def test_ytdlp_extra_args_lets_best_use_default_selector(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_YTDLP_FORMAT="best")
    coll._ffmpeg_available = True

    extra = coll._yt_dlp_extra_args()

    assert "-f" not in extra
    assert extra == ["--merge-output-format", "mp4"]


def test_ytdlp_extra_args_uses_progressive_selector_without_ffmpeg(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_YTDLP_FORMAT="best")
    coll._ffmpeg_available = False

    extra = coll._yt_dlp_extra_args()

    assert extra == ["-f", youtube_mod._YOUTUBE_PROGRESSIVE_FORMAT]


def test_ytdlp_extra_args_preserves_custom_selector(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_YTDLP_FORMAT="bv*+ba/b", YOUTUBE_MERGE_FORMAT="webm")
    coll._ffmpeg_available = True

    extra = coll._yt_dlp_extra_args()

    assert extra == ["-f", "bv*+ba/b", "--merge-output-format", "webm"]


def test_ytdlp_extra_args_adds_max_filesize_guard(monkeypatch):
    coll = _new_collector(
        monkeypatch,
        YOUTUBE_YTDLP_FORMAT="best[height<=720]/best[height<=480]",
        YOUTUBE_MAX_FILESIZE="900M",
    )
    coll._ffmpeg_available = True

    extra = coll._yt_dlp_extra_args()

    assert extra == [
        "-f",
        "best[height<=720]/best[height<=480]",
        "--max-filesize",
        "900M",
        "--merge-output-format",
        "mp4",
    ]


def test_ytdlp_extra_args_skips_merge_without_ffmpeg(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_YTDLP_FORMAT="bv*+ba/b", YOUTUBE_MERGE_FORMAT="webm")
    coll._ffmpeg_available = False

    extra = coll._yt_dlp_extra_args()

    assert extra == ["-f", "bv*+ba/b"]


def test_usable_cookie_file_accepts_netscape_cookie(tmp_path, monkeypatch):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n",
        encoding="utf-8",
    )
    coll = _new_collector(monkeypatch, YOUTUBE_COOKIE_FILE=str(cookie_file))

    assert coll._usable_cookie_file() == str(cookie_file)


def test_usable_cookie_file_ignores_malformed_cookie(tmp_path, monkeypatch):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text('{"cookies": []}\n', encoding="utf-8")
    coll = _new_collector(monkeypatch, YOUTUBE_COOKIE_FILE=str(cookie_file))

    assert coll._usable_cookie_file() == ""


def test_usable_cookie_file_ignores_empty_cookie(tmp_path, monkeypatch, caplog):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("", encoding="utf-8")
    coll = _new_collector(monkeypatch, YOUTUBE_COOKIE_FILE=str(cookie_file))

    with caplog.at_level("WARNING", logger="src.collectors.youtube"):
        assert coll._usable_cookie_file() == ""

    assert "empty cookie file" in caplog.text


@pytest.mark.asyncio
async def test_collect_channel_limits_live_video_downloads(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_VIDEO_DOWNLOADS_PER_TARGET="2")
    coll._has_auth = True
    coll._use_yt_dlp = True
    coll._download_videos = True
    coll._resolve_channel = AsyncMock(return_value=("UC_a", "Channel A"))
    coll._upsert_channel = AsyncMock(return_value=("UUA", 0))
    coll._collect_video_list_via_api = AsyncMock(return_value=["v1", "v2", "v3"])
    coll._select_live_download_video_ids = AsyncMock(return_value=(["v1", "v2"], 0, 0))
    coll._download_videos_via_yt_dlp = AsyncMock(return_value=0)

    await coll._collect_channel("UC_a")

    coll._download_videos_via_yt_dlp.assert_awaited_once_with(
        "UC_a",
        "Channel A",
        ["v1", "v2"],
    )


@pytest.mark.asyncio
async def test_select_live_download_video_ids_filters_before_limit(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_VIDEO_DOWNLOADS_PER_TARGET="2")
    coll._filter_video_ids_for_download = AsyncMock(return_value=(["v1", "v2", "v3", "v4"], 1))
    coll._filter_video_ids_already_archived = AsyncMock(return_value=(["v3", "v4"], 2))

    selected, skipped_duration, skipped_db = await coll._select_live_download_video_ids(
        ["v1", "v2", "v3", "v4", "v5"],
        2,
    )

    assert selected == ["v3", "v4"]
    assert skipped_duration == 1
    assert skipped_db == 2
    coll._filter_video_ids_for_download.assert_awaited_once_with(["v1", "v2", "v3", "v4", "v5"])
    coll._filter_video_ids_already_archived.assert_awaited_once_with(["v1", "v2", "v3", "v4"])


@pytest.mark.asyncio
async def test_collect_runs_media_backlog_before_channel_targets(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_API_KEY="AIzaXX")
    events = []

    async def _backfill():
        events.append("backfill")
        return 1

    async def _collect_channel(target):
        events.append(f"target:{target}")

    coll.run_backfill = AsyncMock(side_effect=_backfill)
    coll._collect_channel = AsyncMock(side_effect=_collect_channel)
    coll.checkpoint.save_progress = AsyncMock()
    coll._use_yt_dlp = True
    coll._download_videos = True

    await coll.collect(["UC_a"])

    assert events == ["backfill", "target:UC_a"]


@pytest.mark.asyncio
async def test_filter_video_ids_for_download_respects_duration_cap(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_MAX_VIDEO_DURATION_MINUTES="10")
    coll.pool._conn.fetch.return_value = [
        {"platform_video_id": "short", "duration": "PT9M59S"},
        {"platform_video_id": "long", "duration": "PT10M1S"},
    ]

    kept, skipped = await coll._filter_video_ids_for_download(["short", "long", "unknown"])

    assert kept == ["short", "unknown"]
    assert skipped == 1


@pytest.mark.asyncio
async def test_filter_video_ids_already_archived_uses_db(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll.pool._conn.fetch.return_value = [
        {"content_id": "video_v1"},
        {"content_id": "video_v3"},
    ]

    kept, skipped = await coll._filter_video_ids_already_archived(["v1", "v2", "v3"])

    assert kept == ["v2"]
    assert skipped == 2
    assert "video_v1" in coll._known_ids
    assert "video_v3" in coll._known_ids


@pytest.mark.asyncio
async def test_video_backfill_groups_are_bounded_and_duration_filtered(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_MAX_VIDEO_DURATION_MINUTES="10")
    coll.pool._conn.fetch.return_value = [
        {
            "platform_video_id": "live_placeholder",
            "duration": "P0D",
            "platform_channel_id": "UC_live",
            "channel_name": "Live Channel",
        },
        {
            "platform_video_id": "too_long",
            "duration": "PT11M",
            "platform_channel_id": "UC_a",
            "channel_name": "Channel A",
        },
        {
            "platform_video_id": "short_1",
            "duration": "PT8M",
            "platform_channel_id": "UC_a",
            "channel_name": "Channel A",
        },
        {
            "platform_video_id": "short_2",
            "duration": "PT1M",
            "platform_channel_id": "UC_b",
            "channel_name": "Channel B",
        },
        {
            "platform_video_id": "short_3",
            "duration": "PT1M",
            "platform_channel_id": "UC_b",
            "channel_name": "Channel B",
        },
    ]

    groups = await coll._get_video_backfill_groups(2)

    assert groups == {
        ("UC_a", "Channel A"): ["short_1"],
        ("UC_b", "Channel B"): ["short_2"],
    }
    sql = coll.pool._conn.fetch.await_args.args[0]
    assert "(v.last_media_attempt_at IS NULL) DESC" in sql
    assert "COALESCE(v.platform_published_at, v.collected_at) ASC" in sql


@pytest.mark.asyncio
async def test_run_backfill_uses_actual_video_insert_count(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_VIDEO_BACKFILL_BATCH_SIZE="10")
    coll._download_videos = True
    coll._use_yt_dlp = True
    coll._get_video_backfill_groups = AsyncMock(
        return_value={
            ("UC_a", "Channel A"): ["v1", "v2"],
            ("UC_b", "Channel B"): ["v3"],
        }
    )
    coll._download_videos_via_yt_dlp = AsyncMock(side_effect=[1, 0])
    coll._progress_count = 999

    out = await coll.run_backfill()

    assert out == 1


@pytest.mark.asyncio
async def test_run_backfill_can_drain_multiple_video_batches(monkeypatch):
    coll = _new_collector(
        monkeypatch,
        YOUTUBE_VIDEO_BACKFILL_BATCH_SIZE="10",
        YOUTUBE_VIDEO_BACKFILL_MAX_PASSES="3",
    )
    coll._download_videos = True
    coll._use_yt_dlp = True
    coll._get_video_backfill_groups = AsyncMock(
        side_effect=[
            {("UC_a", "Channel A"): ["v1", "v2"]},
            {("UC_b", "Channel B"): ["v3"]},
            {},
        ]
    )
    coll._download_videos_via_yt_dlp = AsyncMock(side_effect=[2, 1])

    out = await coll.run_backfill()

    assert out == 3
    assert coll._get_video_backfill_groups.await_count == 3
    assert coll._download_videos_via_yt_dlp.await_count == 2


@pytest.mark.asyncio
async def test_restore_over_duration_candidates_when_cap_disabled(monkeypatch):
    coll = _new_collector(
        monkeypatch,
        YOUTUBE_MAX_VIDEO_DURATION_MINUTES="0",
        YOUTUBE_VIDEO_BACKFILL_SCAN_LIMIT="123",
    )
    coll.pool._conn.fetchval.return_value = 7

    restored = await coll._restore_over_duration_candidates()

    assert restored == 7
    sql, limit = coll.pool._conn.fetchval.await_args.args
    assert "media_skip_reason = 'over_duration_cap'" in sql
    assert "media_status = 'pending'" in sql
    assert limit == 123


@pytest.mark.asyncio
async def test_restore_over_duration_candidates_skips_when_cap_enabled(monkeypatch):
    coll = _new_collector(monkeypatch, YOUTUBE_MAX_VIDEO_DURATION_MINUTES="18")

    restored = await coll._restore_over_duration_candidates()

    assert restored == 0
    coll.pool._conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_base_backfill_does_not_count_false_download_result(monkeypatch):
    coll = _new_collector(monkeypatch)
    coll.get_backfill_items = AsyncMock(
        return_value=[
            {"entity_id": "UC_a", "content_id": "v1", "source_url": "https://example.test/v1"}
        ]
    )
    coll.download_media = AsyncMock(return_value=False)

    out = await BaseCollector.run_backfill(coll)

    assert out == 0


# ── _get_last_scrape_time ────────────────────────────────────────────────


def test_get_last_scrape_time_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_SUBSCRIPTION_CACHE",
                       str(tmp_path / "missing.json"))
    coll = YoutubeCollector()
    assert coll._get_last_scrape_time() is None


def test_get_last_scrape_time_parses_iso(monkeypatch, tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(
        json.dumps({"last_scrape_time": "2024-06-01T00:00:00Z",
                    "subscriptions": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("YOUTUBE_SUBSCRIPTION_CACHE", str(cache))
    coll = YoutubeCollector()
    out = coll._get_last_scrape_time()
    assert out is not None
    assert out.year == 2024 and out.month == 6


# ── module surface ───────────────────────────────────────────────────────


def test_module_exposes_collector_class():
    assert hasattr(youtube_mod, "YoutubeCollector")
    assert youtube_mod.YoutubeCollector.SOURCE_NAME == "youtube"
