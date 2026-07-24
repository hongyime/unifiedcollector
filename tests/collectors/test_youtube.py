"""Tests for src/collectors/youtube.py — Wave 2 batch.

Pure unit. yt-dlp / Google OAuth / httpx are mocked at boundaries.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.collectors import youtube as youtube_mod
from src.collectors.youtube import (
    LIKED_VIDEOS_PLAYLIST_ID,
    YT_API_BASE,
    YoutubeCollector,
    parse_iso8601_duration,
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
    yield


def _make_pool() -> MagicMock:
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)

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


def test_parse_iso8601_duration_empty_or_garbage():
    assert parse_iso8601_duration("") == 0
    assert parse_iso8601_duration(None) == 0  # type: ignore[arg-type]
    # No PT prefix → regex finds nothing matchable → 0
    assert parse_iso8601_duration("garbage") == 0


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
    # Important: no DB write when channel not found
    coll.pool._conn.execute.assert_not_awaited()


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
