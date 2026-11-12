from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core import extractor_fallback as fallback
from src.core.subprocess_downloader import DownloadResult


def _cookie(name: str, domain: str, value: str = "secret-cookie") -> dict:
    return {"name": name, "value": value, "domain": domain, "path": "/", "secure": True, "expires": 1_900_000_000}


def test_canonical_fallback_url_rejects_volatile_and_wrong_first_party_hosts():
    assert fallback.canonical_fallback_url("instagram", "post", "https://cdn.example/a.mp4?Expires=12&Signature=x") is None
    assert fallback.canonical_fallback_url("instagram", "post", "https://example.com/p/a") is None
    assert fallback.canonical_fallback_url("threads", "profile", "https://www.threads.com/@owner") is None
    assert fallback.canonical_fallback_url("instagram", "post", "https://www.instagram.com/p/a/?utm_source=x#fragment") == "https://www.instagram.com/p/a"


def test_source_cookie_filter_and_netscape_jar_keep_only_first_party_values():
    cookies = [_cookie("sessionid", ".instagram.com"), _cookie("SID", ".google.com", "outside")]
    filtered = fallback.filter_vault_cookies("instagram", cookies)
    jar = fallback.netscape_cookie_jar(filtered)
    assert filtered == [cookies[0]]
    assert ".instagram.com\tTRUE\t/\tTRUE\t1900000000\tsessionid\tsecret-cookie" in jar
    assert "outside" not in jar


def test_source_owned_downloaders_receive_only_their_own_vault_cookies():
    cookies = [
        _cookie("sessionid", ".tiktok.com"),
        _cookie("SID", ".youtube.com"),
        _cookie("sessionid", ".instagram.com", "other-source"),
    ]
    assert fallback.filter_vault_cookies("tiktok", cookies) == [cookies[0]]
    assert fallback.filter_vault_cookies("youtube", cookies) == [cookies[1]]


def test_strava_vault_cookie_jar_requires_only_the_strava_session(tmp_path: Path):
    (tmp_path / "latest.json").write_text(
        '{"cookies":[{"name":"_strava4_session","value":"authenticated","domain":".strava.com","path":"/","secure":true},'
        '{"name":"sessionid","value":"outside","domain":".instagram.com","path":"/","secure":true}]}'
    )
    destination = tmp_path / "strava.cookies"
    assert fallback.materialize_vault_cookie_jar("strava", destination, tmp_path)
    jar = destination.read_text()
    assert "_strava4_session" in jar
    assert "outside" not in jar


def test_cookie_jar_uses_newest_snapshot_with_the_source_session_marker(tmp_path: Path):
    (tmp_path / "snapshot_20260101T000000Z.json").write_text(
        '{"cookies":[{"name":"sessionid","value":"authenticated","domain":".instagram.com","path":"/","secure":true}]}'
    )
    (tmp_path / "snapshot_20260102T000000Z.json").write_text(
        '{"cookies":[{"name":"csrftoken","value":"not-a-session","domain":".instagram.com","path":"/","secure":true}]}'
    )

    destination = tmp_path / "cookies.txt"
    assert fallback.materialize_vault_cookie_jar("instagram", destination, tmp_path)
    assert "authenticated" in destination.read_text()
    assert "not-a-session" not in destination.read_text()


def test_temporary_cookie_jar_is_private_deleted_and_not_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    (tmp_path / "latest.json").write_text('{"cookies": [{"name":"sessionid","value":"no-log-secret","domain":".instagram.com","path":"/","secure":true}]}')
    with fallback.temporary_vault_cookie_jar("instagram", tmp_path) as name:
        assert name is not None
        assert (stat_mode := os.stat(name).st_mode & 0o777)
        assert stat_mode == 0o600
        assert "no-log-secret" in Path(name).read_text()
    assert not Path(name).exists()
    assert "no-log-secret" not in caplog.text


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _Connection:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def transaction(self):
        return _Transaction()

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "INSERT 0 1"

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return 1


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_):
        return False


class _Pool:
    def __init__(self):
        self.conn = _Connection()

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_queue_claim_is_source_scoped_and_skip_locked():
    pool = _Pool()
    assert await fallback.claim_extractor_fallback_jobs(pool, "facebook", 3) == []
    query, args = pool.conn.calls[0]
    assert "FOR UPDATE SKIP LOCKED" in query
    assert "source=$1" in query
    assert args == ("facebook", 3)


@pytest.mark.asyncio
async def test_gallery_empty_falls_through_to_ytdlp(monkeypatch, tmp_path: Path):
    job = {"id": 1, "source": "instagram", "content_id": "p1", "target_url": "https://instagram.com/p/p1", "attempts": 1}
    monkeypatch.setattr(fallback, "claim_extractor_fallback_jobs", AsyncMock(return_value=[job]))
    monkeypatch.setattr(fallback, "_finish", AsyncMock())
    monkeypatch.setattr(fallback, "_ingest", AsyncMock(return_value=1))
    monkeypatch.setattr("src.core.source_controls.is_manually_paused", AsyncMock(return_value=None))
    monkeypatch.setattr("src.core.source_controls.automatic_cooldown_seconds", AsyncMock(return_value=0))
    empty = DownloadResult(0, "", "", [], tmp_path, 0.1)
    media = tmp_path / "p1.mp4"
    media.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    downloaded = DownloadResult(0, "", "", [media], tmp_path, 0.1)
    monkeypatch.setattr(fallback, "gallery_dl_download", AsyncMock(return_value=empty))
    monkeypatch.setattr(fallback, "yt_dlp_download", AsyncMock(return_value=downloaded))

    summary = await fallback.run_extractor_fallback(object(), "instagram")

    assert summary["done"] == summary["stored"] == 1
    fallback.yt_dlp_download.assert_awaited_once()
    assert fallback._finish.await_args.kwargs["status"] == "done"


@pytest.mark.asyncio
async def test_fallback_ingest_uses_media_items_compatible_ingest_path(monkeypatch, tmp_path: Path):
    media = tmp_path / "p1.mp4"
    media.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    vault = tmp_path / "vault.mp4"
    vault.write_bytes(media.read_bytes())
    artifact = SimpleNamespace(
        ok=True, path=vault, file_size=vault.stat().st_size, sha256="a" * 64,
        sidecar=SimpleNamespace(relative_path="sidecars/p1.json"),
    )
    monkeypatch.setattr(fallback, "write_atomic_artifact_from_path", lambda **_: artifact)

    pool = _Pool()
    assert await fallback._ingest(pool, {"id": 1, "source": "youtube", "content_id": "p1", "target_url": "https://youtube.com/watch?v=p1"}, [media], "yt-dlp") == 1
    query, _ = pool.conn.calls[0]
    assert "'extractor','post'" in query
    assert len("extractor") <= 16


def test_capability_allowlist_redacts_query_and_rejects_non_listing_airbnb():
    assert fallback.canonical_fallback_url("search", "embed", "https://media.example/watch?id=1&source=feed") == "https://media.example/watch"
    assert fallback.canonical_fallback_url("airbnb", "post", "https://www.airbnb.com/rooms/123?check_in=2026-01-01") == "https://www.airbnb.com/rooms/123"
    assert fallback.canonical_fallback_url("airbnb", "post", "https://www.airbnb.com/users/show/123") is None
    assert fallback.canonical_fallback_url("facebook", "post", "https://www.facebook.com/a") is None
    assert fallback._capability_reason("whatsapp", "attachment") == "private_attachment_native_only"


def test_validated_media_files_require_magic_and_cap_bytes(tmp_path: Path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not video")
    good = tmp_path / "good.mp4"
    good.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    policy = fallback.fallback_capability("instagram")
    assert policy is not None
    assert fallback.validated_media_files([bad, good], policy) == [good]
