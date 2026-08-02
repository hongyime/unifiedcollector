import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from src.bridges import ig_ingest


class _FakeConn:
    def __init__(self):
        self.executes = []
        self.executemany_calls = []
        self.fetches = []
        self.fetchvals = []
        self.fetchrows = []
        self.fetch_result = []
        self.fetchrow_result = None
        self.fetchval_result = None

    async def fetch(self, query, *args):
        self.fetches.append((query, args))
        return self.fetch_result

    async def fetchval(self, query, *args):
        self.fetchvals.append((query, args))
        return self.fetchval_result

    async def fetchrow(self, query, *args):
        self.fetchrows.append((query, args))
        return self.fetchrow_result

    async def execute(self, query, *args):
        self.executes.append((query, args))
        return "INSERT 0 1"

    async def executemany(self, query, args):
        self.executemany_calls.append((query, list(args)))


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return _AcquireContext(self.conn)


class _StuckAcquireContext:
    async def __aenter__(self):
        await asyncio.sleep(10)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _StuckPool:
    def acquire(self):
        return _StuckAcquireContext()


class _FakeRequest(dict):
    def __init__(self, app, body, query=None):
        super().__init__()
        self.app = app
        self._body = body
        self.query = query or {}
        self.headers = {}

    async def json(self):
        return self._body


class _NoDownloadSession:
    def get(self, *args, **kwargs):
        raise AssertionError("extension ingest should not download when vault is unavailable")


class _DownloadResponse:
    def __init__(self, data: bytes):
        self.status = 200
        self.headers = {"content-type": "image/jpeg"}
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def read(self):
        return self._data


class _DownloadSession:
    def __init__(self, data: bytes):
        self.data = data

    def get(self, *args, **kwargs):
        return _DownloadResponse(self.data)


def test_download_headers_include_platform_referers():
    assert ig_ingest._download_headers("tiktok", "https://cdn.example.test/v.jpg", {})["Referer"] == "https://www.tiktok.com/"
    assert ig_ingest._download_headers("facebook", "https://scontent.example.test/i.jpg", {})["Referer"] == "https://www.facebook.com/"
    assert ig_ingest._download_headers("x", "https://pbs.twimg.com/media/x.jpg", {})["Referer"] == "https://x.com/"


def test_tiktok_browser_classifier_queues_short_lived_video_revisit():
    item = {
        "content_type": "video",
        "url": "https://v16m.tiktokcdn.com/video.mp4",
        "meta": {"tiktok_asset_role": "dom_video"},
    }

    outcome, reason, needs_revisit = ig_ingest._classify_tiktok_candidate_result(
        item,
        saved=False,
        browser_result={"reason": "http_403"},
        ingest_mode="browser_upload",
    )

    assert outcome == "short_lived_url"
    assert reason == "http_403"
    assert needs_revisit is True


def test_tiktok_browser_classifier_suppresses_tiny_thumbnails():
    item = {
        "content_type": "photo",
        "url": "https://p16-sign.tiktokcdn.com/avatar.jpg",
        "meta": {"tiktok_asset_role": "dom_image", "width": 80, "height": 80},
    }

    outcome, reason, needs_revisit = ig_ingest._classify_tiktok_candidate_result(
        item,
        saved=False,
        reject_stats={"invalid_media": 1, "examples": {"invalid_media": "too small image"}},
        ingest_mode="url",
    )

    assert outcome == "tiny_thumbnail"
    assert "too small" in reason
    assert needs_revisit is False


def test_tiktok_browser_classifier_marks_duplicates_terminal():
    outcome, reason, needs_revisit = ig_ingest._classify_tiktok_candidate_result(
        {"content_type": "photo", "url": "https://p16-sign.tiktokcdn.com/p.jpg"},
        saved=False,
        reject_stats={"duplicate_sha256": 1},
        ingest_mode="browser_upload",
    )

    assert outcome == "duplicate"
    assert reason == "duplicate_sha256"
    assert needs_revisit is False


def test_tiktok_browser_classifier_marks_saved_terminal():
    outcome, reason, needs_revisit = ig_ingest._classify_tiktok_candidate_result(
        {"content_type": "video", "url": "https://v16m.tiktokcdn.com/video.mp4"},
        saved=True,
        reject_stats={},
        ingest_mode="url",
    )

    assert outcome == "stored"
    assert reason is None
    assert needs_revisit is False


def test_browser_classifier_suppresses_facebook_tiny_thumbnails():
    item = {
        "content_type": "photo",
        "url": "https://static.xx.fbcdn.net/rsrc.php/v4/icon.png",
        "meta": {"facebook_asset_role": "icon", "width": 64, "height": 64},
    }

    outcome, reason, needs_revisit = ig_ingest._classify_browser_candidate_result(
        "facebook",
        item,
        saved=False,
        reject_stats={"invalid_media": 1, "examples": {"invalid_media": "too small image"}},
        ingest_mode="url",
    )

    assert outcome == "tiny_thumbnail"
    assert "too small" in reason
    assert needs_revisit is False


def test_browser_classifier_marks_x_video_fetch_failure_revisitable():
    item = {
        "content_type": "video",
        "url": "https://video.twimg.com/ext_tw_video/123/pu/vid/720x720/a.mp4",
        "meta": {"x_asset_role": "video"},
    }

    outcome, reason, needs_revisit = ig_ingest._classify_browser_candidate_result(
        "x",
        item,
        saved=False,
        browser_result={"reason": "timeout"},
        ingest_mode="browser_upload",
    )

    assert outcome == "browser_fetch_failed"
    assert reason == "timeout"
    assert needs_revisit is True


def test_browser_classifier_queues_deferred_browser_only_media():
    item = {
        "content_type": "photo",
        "url": "https://video.twimg.com/ext_tw_video/123/pu/img/full.jpg",
        "browser_upload_only": True,
        "meta": {"x_asset_role": "image"},
    }

    outcome, reason, needs_revisit = ig_ingest._classify_browser_candidate_result(
        "x",
        item,
        saved=False,
        browser_result={"reason": "deferred_upload_budget"},
        ingest_mode="browser_upload",
    )

    assert outcome == "deferred"
    assert reason == "deferred_upload_budget"
    assert needs_revisit is True


def test_tiktok_classifier_queues_deferred_video_revisit():
    item = {
        "content_type": "video",
        "url": "https://v16-webapp.tiktok.com/video.mp4",
        "browser_upload_only": True,
        "meta": {"tiktok_asset_role": "video_playaddr"},
    }

    outcome, reason, needs_revisit = ig_ingest._classify_tiktok_candidate_result(
        item,
        saved=False,
        browser_result={"reason": "deferred_upload_budget"},
        ingest_mode="browser_upload",
    )

    assert outcome == "deferred"
    assert reason == "deferred_upload_budget"
    assert needs_revisit is True


def test_browser_media_candidates_records_non_tiktok_platform():
    pool = _FakePool()
    response = asyncio.run(
        ig_ingest.browser_media_candidates(
            _FakeRequest(
                {"pool": pool},
                {
                    "platform": "facebook",
                    "username": "feed",
                    "extension_version": "1.21.46",
                    "items": [
                        {
                            "ingest_mode": "browser_upload",
                            "item": {
                                "content_id": "fb_1",
                                "content_type": "photo",
                                "url": "https://scontent.xx.fbcdn.net/v/t39.30808-6/fb.jpg",
                                "meta": {"facebook_asset_role": "background_image"},
                            },
                            "result": {"reason": "http_403", "reject_stats": {"http_status": 1}},
                        }
                    ],
                },
            )
        )
    )
    payload = json.loads(response.text)

    assert payload == {"ok": True, "queued": 1, "platform": "facebook"}
    assert any("browser_media_candidates" in query for query, _args in pool.conn.executes)
    assert not any("tiktok_browser_media_candidates" in query for query, _args in pool.conn.executes)


def test_browser_media_candidates_queues_x_video_revisit():
    pool = _FakePool()
    response = asyncio.run(
        ig_ingest.browser_media_candidates(
            _FakeRequest(
                {"pool": pool},
                {
                    "platform": "x",
                    "username": "timeline",
                    "extension_version": "1.21.50",
                    "items": [
                        {
                            "ingest_mode": "browser_upload",
                            "item": {
                                "content_id": "x_video_1",
                                "content_type": "video",
                                "url": "https://video.twimg.com/ext_tw_video/123/pu/vid/720x720/a.mp4",
                                "meta": {
                                    "x_asset_role": "video",
                                    "author_username": "alice",
                                    "post_id": "123",
                                    "post_url": "https://x.com/alice/status/123",
                                },
                            },
                            "result": {"reason": "timeout", "reject_stats": {"timeout": 1}},
                        }
                    ],
                },
            )
        )
    )
    payload = json.loads(response.text)

    assert payload == {"ok": True, "queued": 1, "platform": "x"}
    queries = [query for query, _args in pool.conn.executes]
    assert any("browser_media_candidates" in query for query in queries)
    assert any("browser_media_revisit_queue" in query for query in queries)
    queue_args = next(args for query, args in pool.conn.executes if "browser_media_revisit_queue" in query)
    assert queue_args[:7] == (
        "x",
        "x_video_1",
        "timeline",
        "https://x.com/alice/status/123",
        "https://video.twimg.com/ext_tw_video/123/pu/vid/720x720/a.mp4",
        "timeout",
        90,
    )


def test_browser_revisit_target_reclaims_stale_claimed(monkeypatch):
    pool = _FakePool()
    pool.conn.fetchrow_result = {
        "platform": "x",
        "content_id": "x_video_1",
        "username": "timeline",
        "post_url": "https://x.com/alice/status/123",
        "source_url": "https://video.twimg.com/ext_tw_video/123/pu/vid/720x720/a.mp4",
        "reason": "timeout",
        "priority": 90,
        "attempts": 2,
        "previous_status": "claimed",
        "metadata": '{"last_claim_previous_status":"claimed"}',
    }
    monkeypatch.setenv("BROWSER_MEDIA_REVISIT_MAX_ATTEMPTS", "7")
    monkeypatch.setattr(ig_ingest, "TIKTOK_BROWSER_REVISIT_CLAIM_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr(ig_ingest, "TIKTOK_BROWSER_REVISIT_CLAIM_HOLD_SECONDS", 60)

    response = asyncio.run(
        ig_ingest.browser_revisit_target(_FakeRequest({"pool": pool}, {}, query={"platform": "x"}))
    )
    payload = json.loads(response.text)

    assert payload["ok"] is True
    assert payload["target"]["platform"] == "x"
    assert payload["target"]["content_id"] == "x_video_1"
    query, args = pool.conn.fetchrows[0]
    assert "browser_media_revisit_queue" in query
    assert "platform = $1" in query
    assert "previous_status" in query
    assert args == ("x", 7, 120, 60)


def test_browser_revisit_result_scopes_update_by_platform():
    pool = _FakePool()
    response = asyncio.run(
        ig_ingest.browser_revisit_result(
            _FakeRequest(
                {"pool": pool},
                {
                    "platform": "x",
                    "content_id": "x_video_1",
                    "status": "success",
                    "reason": "detail_page_harvested",
                    "observed": 2,
                    "stored": 1,
                    "extension_version": "1.21.50",
                },
            )
        )
    )
    payload = json.loads(response.text)

    assert payload == {"ok": True, "status": "completed"}
    query, args = pool.conn.executes[0]
    assert "browser_media_revisit_queue" in query
    assert "WHERE platform = $1 AND content_id = $2" in query
    assert args[:4] == ("x", "x_video_1", "completed", "detail_page_harvested")


def test_tiktok_revisit_target_reclaims_stale_claimed(monkeypatch):
    pool = _FakePool()
    pool.conn.fetchrow_result = {
        "content_id": "video_1",
        "username": "alice",
        "post_url": "https://www.tiktok.com/@alice/video/1",
        "source_url": "https://v16m.tiktokcdn.com/video.mp4",
        "reason": "http_403",
        "priority": 95,
        "attempts": 2,
        "previous_status": "claimed",
        "metadata": '{"last_claim_previous_status":"claimed"}',
    }
    monkeypatch.setenv("TIKTOK_BROWSER_REVISIT_MAX_ATTEMPTS", "7")
    monkeypatch.setattr(ig_ingest, "TIKTOK_BROWSER_REVISIT_CLAIM_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr(ig_ingest, "TIKTOK_BROWSER_REVISIT_CLAIM_HOLD_SECONDS", 60)

    response = asyncio.run(ig_ingest.tiktok_revisit_target(_FakeRequest({"pool": pool}, {})))
    payload = json.loads(response.text)

    assert payload["ok"] is True
    assert payload["target"]["content_id"] == "video_1"
    assert payload["target"]["metadata"]["last_claim_previous_status"] == "claimed"
    query, args = pool.conn.fetchrows[0]
    assert "status = 'claimed'" in query
    assert "previous_status" in query
    assert "COALESCE(last_attempt_at, updated_at, created_at)" in query
    assert args == (7, 120, 60)


def test_extension_ingest_pauses_media_download_when_vault_unavailable(monkeypatch, tmp_path):
    pool = _FakePool()
    monkeypatch.setattr(
        ig_ingest,
        "assert_media_write_allowed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("vault root missing")),
    )

    saved = asyncio.run(
        ig_ingest._download_and_save(
            pool,
            _NoDownloadSession(),
            "instagram",
            "bryan",
            {"content_id": "abc123", "url": "https://example.test/image.jpg"},
        )
    )

    assert saved is False
    assert len(pool.conn.executes) == 1
    query, args = pool.conn.executes[0]
    assert "dead_letter_queue" in query
    assert args[:3] == ("instagram", "bryan", "abc123")
    assert "vault/media unavailable before extension media write" in args[3]


def test_extension_ingest_writes_media_to_vault_blob(monkeypatch, tmp_path):
    pool = _FakePool()
    vault_root = tmp_path / "vault"
    media_root = tmp_path / "media"
    vault_root.mkdir()
    monkeypatch.setattr(ig_ingest, "VAULT_ROOT", vault_root)
    monkeypatch.setattr(ig_ingest, "MEDIA_ROOT", str(media_root))
    monkeypatch.setattr(ig_ingest, "assert_media_write_allowed", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ig_ingest,
        "write_media_sidecar",
        lambda **kwargs: SimpleNamespace(enabled=True, ok=True, relative_path="sidecars/media.json", error=None),
    )
    data = b"\xff\xd8\xff" + (b"a" * 21000)
    digest = hashlib.sha256(data).hexdigest()
    blob = vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    pool.conn.fetchrow_result = {
        "file_path": str(blob),
        "file_size": len(data),
        "sha256": digest,
        "metadata": {
            "vault_sidecar": {
                "ok": True,
                "path": "sidecars/media.json",
            }
        },
    }

    saved = asyncio.run(
        ig_ingest._download_and_save(
            pool,
            _DownloadSession(data),
            "instagram",
            "bryan",
            {
                "content_id": "abc123",
                "kind": "story",
                "url": "https://cdn.example.test/image.jpg",
                "entity_name": "Bryan",
                "meta": {"caption": "hello"},
            },
        )
    )

    assert saved is True
    assert blob.read_bytes() == data
    assert not (media_root / "instagram").exists()
    media_args = next(args for query, args in pool.conn.executes if "INSERT INTO media_items" in query)
    assert media_args[4] == "story_abc123"
    assert Path(media_args[6]) == blob
    metadata = json.loads(media_args[10])
    assert metadata["caption"] == "hello"
    assert metadata["vault_artifact"]["ok"] is True
    assert metadata["vault_artifact"]["blob_path"] == f"media/blobs/{digest[:2]}/{digest[2:4]}/{digest}.jpg"
    assert metadata["vault_sidecar"]["ok"] is True
    assert metadata["vault_sidecar"]["path"] == "sidecars/media.json"
    consistency_updates = [
        args for query, args in pool.conn.executes
        if "vault_artifact_db_consistency" in query or any("vault_artifact_db_consistency" in str(arg) for arg in args)
    ]
    assert consistency_updates
    sidecar = next((vault_root / "sidecars" / "artifacts" / "instagram").rglob("*.json"))
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["metadata"]["ingest_path"] == "extension"
    assert payload["metadata"]["legacy_path"].endswith("story_abc123.jpg")


def test_extension_ingest_accepts_browser_uploaded_media(monkeypatch, tmp_path):
    pool = _FakePool()
    vault_root = tmp_path / "vault"
    media_root = tmp_path / "media"
    vault_root.mkdir()
    monkeypatch.setattr(ig_ingest, "VAULT_ROOT", vault_root)
    monkeypatch.setattr(ig_ingest, "MEDIA_ROOT", str(media_root))
    monkeypatch.setattr(ig_ingest, "assert_media_write_allowed", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ig_ingest,
        "write_media_sidecar",
        lambda **kwargs: SimpleNamespace(enabled=True, ok=True, relative_path="sidecars/media.json", error=None),
    )
    data = b"\xff\xd8\xff" + (b"b" * 22000)
    digest = hashlib.sha256(data).hexdigest()
    blob = vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    pool.conn.fetchrow_result = {
        "file_path": str(blob),
        "file_size": len(data),
        "sha256": digest,
        "metadata": {"vault_sidecar": {"ok": True, "path": "sidecars/media.json"}},
    }

    saved = asyncio.run(
        ig_ingest._download_and_save(
            pool,
            _NoDownloadSession(),
            "tiktok",
            "feed",
            {
                "content_id": "video123",
                "url": "https://v16m.tiktokcdn.com/video.mp4",
                "data_b64": base64.b64encode(data).decode("ascii"),
                "mime_type": "image/jpeg",
                "meta": {"browser_upload": True},
            },
        )
    )

    assert saved is True
    assert blob.read_bytes() == data
    media_args = next(args for query, args in pool.conn.executes if "INSERT INTO media_items" in query)
    metadata = json.loads(media_args[10])
    assert metadata["browser_upload"] is True
    assert metadata["vault_artifact"]["sha256"] == digest


def test_threads_synthetic_media_ids_include_sha_suffix(monkeypatch, tmp_path):
    pool = _FakePool()
    vault_root = tmp_path / "vault"
    media_root = tmp_path / "media"
    vault_root.mkdir()
    monkeypatch.setattr(ig_ingest, "VAULT_ROOT", vault_root)
    monkeypatch.setattr(ig_ingest, "MEDIA_ROOT", str(media_root))
    monkeypatch.setattr(ig_ingest, "assert_media_write_allowed", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ig_ingest,
        "write_media_sidecar",
        lambda **kwargs: SimpleNamespace(enabled=True, ok=True, relative_path="sidecars/media.json", error=None),
    )

    async def fake_verify_media_item_db_consistency(conn, **kwargs):
        return SimpleNamespace(ok=True, errors=())

    monkeypatch.setattr(
        ig_ingest,
        "verify_media_item_db_consistency",
        fake_verify_media_item_db_consistency,
    )
    first = b"\xff\xd8\xff" + (b"a" * 22000)
    second = b"\xff\xd8\xff" + (b"b" * 23000)
    first_sha = hashlib.sha256(first).hexdigest()
    second_sha = hashlib.sha256(second).hexdigest()

    saved_first = asyncio.run(
        ig_ingest._download_and_save(
            pool,
            _DownloadSession(first),
            "threads",
            "foryou",
            {"content_id": "img_same", "url": "https://cdn.example.test/first.jpg"},
        )
    )
    saved_second = asyncio.run(
        ig_ingest._download_and_save(
            pool,
            _DownloadSession(second),
            "threads",
            "foryou",
            {"content_id": "img_same", "url": "https://cdn.example.test/second.jpg"},
        )
    )

    assert saved_first is True
    assert saved_second is True
    media_args = [args for query, args in pool.conn.executes if "INSERT INTO media_items" in query]
    content_ids = [args[4] for args in media_args]
    assert content_ids == [f"img_same_{first_sha[:12]}", f"img_same_{second_sha[:12]}"]
    assert content_ids[0] != content_ids[1]
    assert not any(
        "vault artifact db consistency failed" in str(args)
        for query, args in pool.conn.executes
        if "dead_letter_queue" in query
    )


def test_archive_browser_capture_writes_profile_raw_payload(monkeypatch):
    pool = _FakePool()
    calls = []

    def fake_write_raw_payload(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr(ig_ingest, "write_raw_payload", fake_write_raw_payload)

    asyncio.run(
        ig_ingest._archive_browser_capture(
            pool,
            "instagram",
            "profile",
            {
                "platform": "instagram",
                "extension_version": "1.21.19",
                "owner": {"username": "bryan"},
                "profile": {"username": "alice", "user_id": "123"},
            },
        )
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["source"] == "instagram"
    assert call["artifact_id"].startswith("extension/profile/alice/")
    assert call["target_tables"] == ["instagram_profiles"]
    assert call["payload"]["profile"]["username"] == "alice"
    assert call["metadata"]["ingest_path"] == "extension"
    assert call["metadata"]["collection_account"] == "bryan"
    assert call["extension"] == "json.gz"
    assert call["metadata"]["extension_version"] == "1.21.19"
    assert pool.conn.executes == []


def test_record_browser_ingest_event_writes_observed_and_stored_counts():
    pool = _FakePool()

    asyncio.run(
        ig_ingest._record_browser_ingest_event(
            pool,
            "threads",
            "media",
            "feed",
            observed_count=12,
            stored_count=3,
            metadata={"extension_version": "1.21.20"},
        )
    )

    assert len(pool.conn.executes) == 2
    query, args = pool.conn.executes[0]
    assert "browser_ingest_events" in query
    assert args[:5] == ("threads", "media", "feed", 12, 3)
    assert "extension_version" in args[5]
    health_query, health_args = pool.conn.executes[1]
    assert "source_health" in health_query
    assert health_args == ("threads",)


def test_record_browser_ingest_event_does_not_mark_heartbeat_success():
    pool = _FakePool()

    asyncio.run(
        ig_ingest._record_browser_ingest_event(
            pool,
            "threads",
            "browser_heartbeat",
            "tab-1",
            observed_count=1,
            stored_count=0,
            metadata={"extension_version": "1.21.52"},
        )
    )

    assert len(pool.conn.executes) == 1
    query, args = pool.conn.executes[0]
    assert "browser_ingest_events" in query
    assert "source_health" not in query
    assert args[:5] == ("threads", "browser_heartbeat", "tab-1", 1, 0)


def test_record_browser_ingest_event_marks_empty_probe_success():
    pool = _FakePool()

    asyncio.run(
        ig_ingest._record_browser_ingest_event(
            pool,
            "x",
            "media",
            "timeline",
            observed_count=0,
            stored_count=0,
            metadata={
                "extension_version": "1.22.4",
                "probe_reason": "no_dom_media_candidates",
            },
        )
    )

    assert len(pool.conn.executes) == 2
    query, args = pool.conn.executes[0]
    assert "browser_ingest_events" in query
    assert args[:5] == ("x", "media", "timeline", 0, 0)
    health_query, health_args = pool.conn.executes[1]
    assert "source_health" in health_query
    assert health_args == ("x",)


def test_browser_upload_duplicate_counts_as_accepted(monkeypatch):
    pool = _FakePool()
    events = []

    async def fake_download(_pool, _session, _platform, _username, _item, reject_stats):
        reject_stats["duplicate_content_id"] = 1
        return False

    async def fake_event(_pool, platform, endpoint, subject, **kwargs):
        events.append((platform, endpoint, subject, kwargs))

    monkeypatch.setattr(ig_ingest, "_download_and_save", fake_download)
    monkeypatch.setattr(ig_ingest, "_record_browser_ingest_event", fake_event)

    app = {
        "pool": pool,
        "session": object(),
        "sem": asyncio.Semaphore(1),
    }
    resp = asyncio.run(
        ig_ingest._ingest_uploaded_media(
            app,
            "instagram",
            {
                "username": "alice",
                "extension_version": "1.21.42",
                "file_size": 12345,
                "mime_type": "image/jpeg",
                "item": {
                    "content_id": "story_abc123",
                    "url": "https://scontent.cdninstagram.com/v/t51.29350-15/abc.jpg",
                },
                "data_b64": base64.b64encode(b"\xff\xd8\xff" + b"x" * 21000).decode("ascii"),
            },
        )
    )

    assert resp["accepted"] == 1
    assert resp["stored"] == 1
    assert resp["saved"] == 0
    assert resp["deduped"] is True
    assert resp["reject_stats"] == {"duplicate_content_id": 1}
    assert events == [
        (
            "instagram",
            "media",
            "alice",
            {
                "observed_count": 1,
                "stored_count": 1,
                "metadata": {
                    "extension_version": "1.21.42",
                    "ingest_mode": "browser_upload",
                    "accepted": True,
                    "saved": False,
                    "deduped": True,
                    "content_id": "story_abc123",
                    "file_size": 12345,
                    "mime_type": "image/jpeg",
                    "reject_stats": {"duplicate_content_id": 1},
                },
            },
        )
    ]


def test_drain_propagates_extension_version_to_media_and_telemetry(monkeypatch):
    pool = _FakePool()
    saved_items = []
    events = []

    async def fake_download(_pool, _session, platform, username, item, reject_stats):
        saved_items.append((platform, username, item, dict(reject_stats)))
        return True

    async def fake_event(_pool, platform, endpoint, subject, **kwargs):
        events.append((platform, endpoint, subject, kwargs))

    monkeypatch.setattr(ig_ingest, "_download_and_save", fake_download)
    monkeypatch.setattr(ig_ingest, "_record_browser_ingest_event", fake_event)

    app = {
        "pool": pool,
        "session": object(),
        "sem": asyncio.Semaphore(2),
    }

    asyncio.run(
        ig_ingest._drain(
            app,
            "instagram",
            "alice",
            [{"content_id": "m1", "url": "https://cdn/x.jpg", "meta": {"role": "story"}}],
            "1.21.35",
        )
    )

    assert saved_items[0][2]["meta"] == {"role": "story", "extension_version": "1.21.35"}
    assert events == [
        (
            "instagram",
            "media",
            "alice",
            {
                "observed_count": 1,
                "stored_count": 1,
                "metadata": {"extension_version": "1.21.35"},
            },
        )
    ]


def test_ingest_records_explicit_empty_media_probe(monkeypatch):
    pool = _FakePool()
    events = []

    async def fake_event(_pool, platform, endpoint, subject, **kwargs):
        events.append((platform, endpoint, subject, kwargs))

    monkeypatch.setattr(ig_ingest, "_record_browser_ingest_event", fake_event)

    resp = asyncio.run(
        ig_ingest._ingest(
            {"pool": pool},
            "x",
            {
                "username": "timeline",
                "items": [],
                "record_empty": True,
                "extension_version": "1.21.38",
                "probe_reason": "no_dom_media_candidates",
                "probe_meta": {"feed": "home/following", "posts": 5},
            },
        )
    )

    assert resp == {"accepted": 0, "platform": "x"}
    assert events == [
        (
            "x",
            "media",
            "timeline",
            {
                "observed_count": 0,
                "stored_count": 0,
                "metadata": {
                    "extension_version": "1.21.38",
                    "probe_reason": "no_dom_media_candidates",
                    "probe_meta": {"feed": "home/following", "posts": 5},
                },
            },
        )
    ]


def test_empty_media_probe_marks_browser_content_progress():
    assert ig_ingest._browser_event_marks_source_success(
        "facebook",
        "media",
        0,
        0,
        {"probe_reason": "no_dom_media_candidates"},
    )
    assert not ig_ingest._browser_event_marks_source_success(
        "facebook",
        "media",
        0,
        0,
        {"probe_reason": "manual_backend_probe"},
    )
    assert not ig_ingest._browser_event_marks_source_success(
        "facebook",
        "media",
        0,
        0,
        {"probe_reason": "forced_recovery_started"},
    )
    assert not ig_ingest._browser_event_marks_source_success(
        "facebook",
        "media",
        0,
        0,
        {},
    )


def test_browser_heartbeat_handler_records_platform_loop():
    pool = _FakePool()
    req = _FakeRequest(
        {"pool": pool},
        {
            "platform": "Twitter / X",
            "label": "Twitter / X",
            "running": True,
            "url": "https://x.com/home",
            "tab_id": 123,
            "extension_version": "1.21.29",
        },
    )

    resp = asyncio.run(ig_ingest.browser_heartbeat_handler(req))

    assert resp.status == 200
    query, args = pool.conn.executes[0]
    assert "browser_ingest_events" in query
    assert args[:5] == ("x", "browser_heartbeat", "123", 1, 0)
    assert "1.21.29" in args[5]


def test_browser_heartbeat_handler_records_scraper_tab_summary_metadata():
    pool = _FakePool()
    req = _FakeRequest(
        {"pool": pool},
        {
            "platform": "bridge",
            "label": "UnifiedCollector Bridge",
            "running": True,
            "tab_id": "scraper_tabs",
            "extension_version": "1.21.99",
            "health_status": "scraper_heartbeat_degraded",
            "health_reason": "watchdog",
            "scraper_tabs_seen": 7,
            "scraper_tabs_sent": 2,
            "scraper_tabs_failed": 5,
            "scraper_tabs_canonical": 6,
            "scraper_tabs_skipped": 1,
            "scraper_heartbeat_error": "HTTP 500",
        },
    )

    resp = asyncio.run(ig_ingest.browser_heartbeat_handler(req))

    assert resp.status == 200
    query, args = pool.conn.executes[0]
    assert "browser_ingest_events" in query
    assert args[:5] == ("bridge", "browser_heartbeat", "scraper_tabs", 1, 0)
    metadata = json.loads(args[5])
    assert metadata["health_status"] == "scraper_heartbeat_degraded"
    assert metadata["scraper_tabs_seen"] == 7
    assert metadata["scraper_tabs_sent"] == 2
    assert metadata["scraper_tabs_failed"] == 5
    assert metadata["scraper_tabs_canonical"] == 6
    assert metadata["scraper_tabs_skipped"] == 1
    assert metadata["scraper_heartbeat_error"] == "HTTP 500"


def test_browser_heartbeat_handler_records_page_recovery_metadata():
    pool = _FakePool()
    req = _FakeRequest(
        {"pool": pool},
        {
            "platform": "tiktok",
            "label": "TikTok",
            "running": True,
            "url": "https://www.tiktok.com/following",
            "tab_id": 321,
            "extension_version": "1.21.36",
            "health_status": "recoverable_error_shell",
            "health_reason": "sorry_could_not_show_page",
            "text_sample": "Sorry, we couldn't show that page",
            "recovery_scheduled": True,
            "recovery_attempt": 1,
            "recovery_delay_ms": 90000,
        },
    )

    resp = asyncio.run(ig_ingest.browser_heartbeat_handler(req))

    assert resp.status == 200
    query, args = pool.conn.executes[0]
    assert "browser_ingest_events" in query
    assert args[:5] == ("tiktok", "browser_heartbeat", "321", 1, 0)
    metadata = json.loads(args[5])
    assert metadata["health_status"] == "recoverable_error_shell"
    assert metadata["health_reason"] == "sorry_could_not_show_page"
    assert metadata["recovery_scheduled"] is True
    assert metadata["recovery_attempt"] == 1
    assert metadata["extension_version"] == "1.21.36"


def test_browser_heartbeat_handler_records_forced_cycle_metadata():
    pool = _FakePool()
    req = _FakeRequest(
        {"pool": pool},
        {
            "platform": "threads",
            "label": "Threads",
            "running": True,
            "url": "https://www.threads.com/",
            "tab_id": 456,
            "extension_version": "1.21.72",
            "health_status": "forced_cycle_finished",
            "health_reason": "browser_content_stale",
            "cycle_reason": "browser_content_stale",
            "cycle_targets": 1,
            "cycle_saved": 7,
            "cycle_discovered": 3,
            "loop_running": True,
            "one_shot_running": True,
            "one_shot_age_ms": 1234,
            "stale_after_ms": 480000,
            "one_shot_timeout": True,
            "timeout_ms": 180000,
        },
    )

    resp = asyncio.run(ig_ingest.browser_heartbeat_handler(req))

    assert resp.status == 200
    query, args = pool.conn.executes[0]
    assert "browser_ingest_events" in query
    assert args[:5] == ("threads", "browser_heartbeat", "456", 1, 0)
    metadata = json.loads(args[5])
    assert metadata["health_status"] == "forced_cycle_finished"
    assert metadata["cycle_reason"] == "browser_content_stale"
    assert metadata["cycle_targets"] == 1
    assert metadata["cycle_saved"] == 7
    assert metadata["cycle_discovered"] == 3
    assert metadata["loop_running"] is True
    assert metadata["one_shot_running"] is True
    assert metadata["one_shot_age_ms"] == 1234
    assert metadata["stale_after_ms"] == 480000
    assert metadata["one_shot_timeout"] is True
    assert metadata["timeout_ms"] == 180000


def test_browser_heartbeat_handler_records_bridge_diagnostic_platform():
    pool = _FakePool()
    req = _FakeRequest(
        {"pool": pool},
        {
            "platform": "bridge",
            "label": "UnifiedCollector Bridge",
            "running": True,
            "url": "chrome-extension://abc/background.js",
            "tab_id": "service_worker",
            "extension_version": "1.21.37",
            "health_status": "service_worker_active",
            "health_reason": "warm_start",
        },
    )

    resp = asyncio.run(ig_ingest.browser_heartbeat_handler(req))

    assert resp.status == 200
    query, args = pool.conn.executes[0]
    assert "browser_ingest_events" in query
    assert args[:5] == ("bridge", "browser_heartbeat", "service_worker", 1, 0)
    metadata = json.loads(args[5])
    assert metadata["health_status"] == "service_worker_active"
    assert metadata["health_reason"] == "warm_start"
    assert metadata["extension_version"] == "1.21.37"


def test_browser_heartbeat_handler_returns_when_telemetry_db_is_stuck(monkeypatch):
    monkeypatch.setattr(ig_ingest, "BROWSER_TELEMETRY_WRITE_TIMEOUT_SECONDS", 0.01)
    req = _FakeRequest(
        {"pool": _StuckPool()},
        {
            "platform": "bridge",
            "label": "UnifiedCollector Bridge",
            "running": True,
            "url": "chrome-extension://abc/background.js",
            "tab_id": "service_worker",
            "extension_version": "1.21.63",
            "health_status": "manual_ingest_probe",
        },
    )

    resp = asyncio.run(ig_ingest.browser_heartbeat_handler(req))

    assert resp.status == 200


def test_dm_hook_heartbeat_fails_open_when_db_is_stuck(monkeypatch):
    monkeypatch.setattr(ig_ingest, "DM_HOOK_HEARTBEAT_WRITE_TIMEOUT_SECONDS", 0.01)
    req = _FakeRequest(
        {"pool": _StuckPool()},
        {
            "platform": "tiktok",
            "owner": "bryanseah234",
            "probes_sent": 1,
            "samples_shipped": 0,
            "extension_version": "1.21.92",
        },
    )

    resp = asyncio.run(ig_ingest.dm_hook_heartbeat_handler(req))

    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload["ok"] is True
    assert payload["recorded"] is False
    assert payload["telemetry_degraded"] is True
    assert payload["reason"] == "db_write_timeout"


def test_ig_cooldown_fails_open_when_db_is_stuck(monkeypatch):
    monkeypatch.setattr(ig_ingest, "IG_COOLDOWN_READ_TIMEOUT_SECONDS", 0.01)
    req = _FakeRequest(
        {"pool": _StuckPool()},
        {},
        query={"account": "4495993191"},
    )

    resp = asyncio.run(ig_ingest.ig_cooldown(req))

    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload["cooling"] is False
    assert payload["secs_left"] == 0
    assert payload["account"] == "4495993191"
    assert payload["cooldown_degraded"] is True


def test_structured_browser_capture_paths_use_structured_timeout(monkeypatch):
    monkeypatch.setattr(ig_ingest, "SOCIAL_INGEST_REQUEST_TIMEOUT_SECONDS", 8.0)
    monkeypatch.setattr(ig_ingest, "SOCIAL_INGEST_STRUCTURED_REQUEST_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(ig_ingest, "SOCIAL_INGEST_HEARTBEAT_REQUEST_TIMEOUT_SECONDS", 31.0)
    monkeypatch.setattr(ig_ingest, "SOCIAL_INGEST_UPLOAD_REQUEST_TIMEOUT_SECONDS", 60.0)

    for path in (
        "/social/posts",
        "/social/profile",
        "/social/users",
        "/social/seed",
        "/social/strava-streams",
        "/social/browser-media-candidates",
    ):
        assert ig_ingest._request_timeout_seconds(path) == 30.0

    assert ig_ingest._request_timeout_seconds("/social/browser-heartbeat") == 31.0
    assert ig_ingest._request_timeout_seconds("/social/ingest-upload") == 60.0
    assert ig_ingest._request_timeout_seconds("/social/targets") == 8.0


def test_posts_handler_queues_structured_ingest_without_waiting(monkeypatch):
    scheduled = []

    def fake_schedule(app, coro, label):
        scheduled.append((label, coro))
        coro.close()

    monkeypatch.setattr(ig_ingest, "_schedule_app_task", fake_schedule)
    req = _FakeRequest(
        {"pool": _FakePool(), "tasks": set()},
        {
            "platform": "threads",
            "username": "following",
            "posts": [{"platform_post_id": "p1", "author_username": "alice"}],
        },
    )

    resp = asyncio.run(ig_ingest.posts_handler(req))

    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload == {"ok": True, "queued": True, "observed": 1, "saved": 0}
    assert scheduled[0][0] == "browser_posts_ingest"


def test_posts_background_ingest_keeps_archive_save_event_and_author_writes(monkeypatch):
    calls = []

    async def fake_archive(pool, platform, kind, body):
        calls.append(("archive", platform, kind, body["username"]))

    async def fake_save(pool, platform, posts):
        calls.append(("save", platform, [p["platform_post_id"] for p in posts]))
        return len(posts)

    async def fake_event(pool, platform, endpoint, username, observed_count, stored_count):
        calls.append(("event", platform, endpoint, username, observed_count, stored_count))

    async def fake_users(pool, platform, users, context, owner=None):
        calls.append(("users", platform, users, context, owner))
        return len(users)

    monkeypatch.setattr(ig_ingest, "_archive_browser_capture", fake_archive)
    monkeypatch.setattr(ig_ingest, "_save_posts", fake_save)
    monkeypatch.setattr(ig_ingest, "_record_browser_ingest_event", fake_event)
    monkeypatch.setattr(ig_ingest, "_record_users", fake_users)
    app = {
        "pool": _FakePool(),
        "structured_sem": asyncio.Semaphore(1),
    }
    body = {
        "username": "timeline",
        "posts": [
            {"platform_post_id": "p1", "author_username": "alice"},
            {"platform_post_id": "p2"},
        ],
    }

    asyncio.run(ig_ingest._posts_ingest_background(app, "x", body))

    assert calls == [
        ("archive", "x", "posts", "timeline"),
        ("save", "x", ["p1", "p2"]),
        ("event", "x", "posts", "timeline", 2, 2),
        ("users", "x", [{"username": "alice"}], "author", None),
    ]


def test_record_users_batches_user_and_follow_edge_upserts():
    pool = _FakePool()

    recorded = asyncio.run(ig_ingest._record_users(
        pool,
        "instagram",
        [
            {"user_id": "100", "username": "alpha", "full_name": "Alpha"},
            {"username": "beta", "profile_pic_url": "https://example.test/beta.jpg"},
        ],
        "follow",
        owner="me",
    ))

    assert recorded == 2
    assert len(pool.conn.executemany_calls) == 2
    user_query, user_args = pool.conn.executemany_calls[0]
    assert "INSERT INTO social_users" in user_query
    assert user_args[0][:4] == ("instagram", "100", "100", "alpha")
    assert user_args[1][:4] == ("instagram", "beta", None, "beta")
    edge_query, edge_args = pool.conn.executemany_calls[1]
    assert "INSERT INTO follow_edges" in edge_query
    assert edge_args == [
        ("instagram", "me", "100", "following", "alpha"),
        ("instagram", "me", "beta", "following", "beta"),
    ]


def test_targets_refresh_side_caches_once_per_ttl(monkeypatch):
    calls = []

    async def fake_refresh_proximity(pool):
        calls.append("proximity")

    async def fake_refresh_priority(pool):
        calls.append("priority")

    monkeypatch.setattr(ig_ingest, "SOCIAL_TARGET_CACHE_REFRESH_SECONDS", 300)
    monkeypatch.setattr(ig_ingest, "SOCIAL_TARGET_CACHE_REFRESH_ON_REQUEST", True)
    monkeypatch.setattr(ig_ingest, "_SOCIAL_TARGET_CACHE_REFRESH_LAST", 0.0)
    monkeypatch.setattr(ig_ingest, "_SOCIAL_TARGET_CACHE_REFRESH_LOCK", None)
    monkeypatch.setattr(ig_ingest, "refresh_account_proximity_cache", fake_refresh_proximity)
    monkeypatch.setattr(ig_ingest, "refresh_collector_priority_hints", fake_refresh_priority)
    pool = _FakePool()

    async def run_twice():
        await ig_ingest._targets_for(pool, "instagram")
        await ig_ingest._targets_for(pool, "instagram")

    asyncio.run(run_twice())

    assert calls == ["proximity", "priority"]


def test_cached_targets_for_reuses_response_inside_ttl(monkeypatch):
    calls = []

    async def fake_targets(pool, platform):
        calls.append(platform)
        return [{"username": "alpha", "hop": 0}]

    monkeypatch.setattr(ig_ingest, "SOCIAL_TARGET_RESPONSE_CACHE_SECONDS", 45.0)
    monkeypatch.setattr(ig_ingest, "_targets_for", fake_targets)
    ig_ingest._SOCIAL_TARGET_RESPONSE_CACHE.clear()
    ig_ingest._SOCIAL_TARGET_RESPONSE_LOCKS.clear()
    pool = _FakePool()

    async def run_twice():
        first = await ig_ingest._cached_targets_for(pool, "instagram")
        second = await ig_ingest._cached_targets_for(pool, "instagram")
        return first, second

    first, second = asyncio.run(run_twice())

    assert first == [{"username": "alpha", "hop": 0}]
    assert second == first
    assert calls == ["instagram"]


def test_browser_heartbeat_handler_reports_degraded_when_pool_missing():
    req = _FakeRequest(
        {"pool": None},
        {
            "platform": "bridge",
            "label": "UnifiedCollector Bridge",
            "running": True,
            "url": "chrome-extension://abc/background.js",
            "tab_id": "service_worker",
            "extension_version": "1.21.63",
            "health_status": "manual_ingest_probe",
        },
    )

    resp = asyncio.run(ig_ingest.browser_heartbeat_handler(req))

    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload["telemetry_degraded"] is True


def test_browser_heartbeat_handler_requests_extension_reload_for_old_version(monkeypatch):
    monkeypatch.setattr(ig_ingest, "UC_EXTENSION_EXPECTED_VERSION", "1.23.10")
    req = _FakeRequest(
        {"pool": None},
        {
            "platform": "bridge",
            "label": "UnifiedCollector Bridge",
            "running": True,
            "tab_id": "service_worker",
            "extension_version": "1.23.9",
        },
    )

    resp = asyncio.run(ig_ingest.browser_heartbeat_handler(req))

    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload["expected_extension_version"] == "1.23.10"
    assert payload["current_extension_version"] == "1.23.9"
    assert payload["reload_extension"] is True
    assert payload["reload_reason"] == "extension_version_mismatch"


def test_browser_heartbeat_handler_does_not_reload_current_extension(monkeypatch):
    monkeypatch.setattr(ig_ingest, "UC_EXTENSION_EXPECTED_VERSION", "1.23.10")
    req = _FakeRequest(
        {"pool": None},
        {
            "platform": "bridge",
            "label": "UnifiedCollector Bridge",
            "running": True,
            "tab_id": "service_worker",
            "extension_version": "v1.23.10",
        },
    )

    resp = asyncio.run(ig_ingest.browser_heartbeat_handler(req))

    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload["expected_extension_version"] == "1.23.10"
    assert "reload_extension" not in payload
    assert "current_extension_version" not in payload


def test_browser_heartbeat_handler_does_not_reload_newer_extension(monkeypatch):
    monkeypatch.setattr(ig_ingest, "UC_EXTENSION_EXPECTED_VERSION", "1.23.10")
    req = _FakeRequest(
        {"pool": None},
        {
            "platform": "bridge",
            "label": "UnifiedCollector Bridge",
            "running": True,
            "tab_id": "service_worker",
            "extension_version": "1.23.11",
        },
    )

    resp = asyncio.run(ig_ingest.browser_heartbeat_handler(req))

    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload["expected_extension_version"] == "1.23.10"
    assert "reload_extension" not in payload
    assert "current_extension_version" not in payload


def test_browser_heartbeat_handler_requests_forced_cycle_when_content_stale(monkeypatch):
    monkeypatch.setattr(ig_ingest, "BROWSER_CONTENT_STALE_SECONDS", 3600)
    ig_ingest._BROWSER_CONTENT_HINT_CACHE.clear()
    pool = _FakePool()
    pool.conn.fetchrow_result = {"age_seconds": 7200}
    req = _FakeRequest(
        {"pool": pool},
        {
            "platform": "x",
            "label": "Twitter / X",
            "running": True,
            "url": "https://x.com/home",
            "tab_id": 123,
            "extension_version": "1.21.64",
        },
    )

    resp = asyncio.run(ig_ingest.browser_heartbeat_handler(req))

    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload["force_cycle"] is True
    assert payload["force_reason"] == "browser_content_stale"
    assert payload["content_age_seconds"] == 7200
    query, args = pool.conn.fetchrows[0]
    assert "metadata ? 'probe_reason'" in query
    assert "manual_backend_probe" in query
    assert "forced_recovery_started" in query
    assert "ORDER BY created_at DESC" in query
    assert args == ("x",)


def test_browser_heartbeat_handler_fails_active_when_content_hint_is_slow(monkeypatch):
    async def slow_hint(pool, platform):
        await asyncio.sleep(10)
        return {"force_cycle": True}

    monkeypatch.setattr(ig_ingest, "BROWSER_CONTENT_HINT_RESPONSE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(ig_ingest, "_browser_content_recovery_hint", slow_hint)
    req = _FakeRequest(
        {"pool": _FakePool()},
        {
            "platform": "x",
            "label": "Twitter / X",
            "running": True,
            "url": "https://x.com/home",
            "tab_id": 123,
            "extension_version": "1.21.75",
        },
    )

    resp = asyncio.run(ig_ingest.browser_heartbeat_handler(req))

    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload["force_cycle"] is True
    assert payload["force_reason"] == "content_age_response_budget_exceeded"
    assert payload["stale_after_seconds"] == ig_ingest.BROWSER_CONTENT_STALE_SECONDS


def test_browser_content_hint_returns_pending_during_inflight_check():
    ig_ingest._BROWSER_CONTENT_HINT_CACHE.clear()
    ig_ingest._BROWSER_CONTENT_HINT_INFLIGHT.add("x")
    pool = _FakePool()
    try:
        hint = asyncio.run(ig_ingest._browser_content_recovery_hint(pool, "x"))
    finally:
        ig_ingest._BROWSER_CONTENT_HINT_INFLIGHT.discard("x")

    assert hint == {"force_cycle": False, "force_reason": "content_age_check_pending"}
    assert pool.conn.fetchrows == []


def test_record_strava_stream_http_429_writes_rate_limit_event(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(ig_ingest, "STRAVA_BROWSER_429_COOLDOWN_SECONDS", 1234)

    async def fake_record_dynamic_cooldown(*args, **kwargs):
        return SimpleNamespace(
            seconds_remaining=1234,
            service="rate_limit:strava:gps_streams:bryanseah234",
            streak=1,
        )

    monkeypatch.setattr(
        ig_ingest,
        "record_dynamic_cooldown",
        fake_record_dynamic_cooldown,
    )

    recorded = asyncio.run(
        ig_ingest._record_strava_stream_http_event(
            pool,
            {
                "activity_id": "19283135496",
                "request_url": "https://www.strava.com/activities/19283135496/streams",
                "http_status": 429,
                "owner": "bryanseah234",
                "extension_version": "1.21.24",
            },
        )
    )

    assert recorded is True
    query, args = next(
        (query, args)
        for query, args in pool.conn.executes
        if "rate_limit_events" in query
    )
    assert "rate_limit_events" in query
    assert args[:6] == (
        "strava",
        "bryanseah234",
        "browser_strava_streams",
        429,
        1234,
        "browser Strava stream HTTP 429 for 19283135496",
    )
    assert "19283135496" in args[6]


def test_record_strava_stream_http_429_does_not_extend_active_duplicate_cooldown(monkeypatch):
    pool = _FakePool()
    pool.conn.fetchval_result = True
    monkeypatch.setattr(ig_ingest, "STRAVA_BROWSER_429_COOLDOWN_SECONDS", 1234)

    recorded = asyncio.run(
        ig_ingest._record_strava_stream_http_event(
            pool,
            {
                "activity_id": "19283135496",
                "request_url": "https://www.strava.com/activities/19283135496/streams",
                "http_status": 429,
                "owner": "bryanseah234",
                "extension_version": "1.21.33",
            },
        )
    )

    assert recorded is False
    assert pool.conn.executes == []
    query, args = pool.conn.fetchvals[0]
    assert "UPDATE rate_limit_events" in query
    assert "duplicate_suppressed_count" in query
    assert args[:3] == ("bryanseah234", "19283135496", 429)
    assert args[3] == "https://www.strava.com/activities/19283135496/streams"


def test_archive_browser_capture_writes_dm_sample_raw_payload(monkeypatch):
    pool = _FakePool()
    calls = []

    def fake_write_raw_payload(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr(ig_ingest, "write_raw_payload", fake_write_raw_payload)

    asyncio.run(
        ig_ingest._archive_browser_capture(
            pool,
            "tiktok",
            "dm_sample",
            {
                "platform": "tiktok",
                "owner": "bryan",
                "transport": "websocket",
                "frame_kind": "binary",
                "frame_size": 2048,
                "b64": "AAAA",
                "decoded_bytes": 3,
            },
        )
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["source"] == "tiktok"
    assert call["artifact_id"].startswith("extension/dm_sample/bryan_websocket_binary_2048/")
    assert call["target_tables"] == ["dm_probe_log"]
    assert call["payload"]["b64"] == "AAAA"
    assert call["metadata"]["endpoint"] == "dm_sample"
    assert call["metadata"]["body_keys"] == [
        "b64",
        "decoded_bytes",
        "frame_kind",
        "frame_size",
        "owner",
        "platform",
        "transport",
    ]
    assert pool.conn.executes == []


def test_archive_browser_capture_writes_decoded_dm_target_hints(monkeypatch):
    pool = _FakePool()
    calls = []

    def fake_write_raw_payload(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr(ig_ingest, "write_raw_payload", fake_write_raw_payload)

    asyncio.run(
        ig_ingest._archive_browser_capture(
            pool,
            "tiktok",
            "dm_decoded",
            {
                "platform": "tiktok",
                "owner": "72101656",
                "threads": [{"conversation_id": "0:1:1:2"}],
                "messages": [{"message_id": "9988", "text": "hello"}],
            },
        )
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["artifact_id"].startswith("extension/dm_decoded/9988/")
    assert call["target_tables"] == ["tiktok_dm_thread", "tiktok_dm"]
    assert call["payload"]["messages"][0]["message_id"] == "9988"
    assert call["metadata"]["collection_account"] == "72101656"


def test_archive_browser_capture_writes_strava_stream_raw_payload(monkeypatch):
    pool = _FakePool()
    calls = []

    def fake_write_raw_payload(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr(ig_ingest, "write_raw_payload", fake_write_raw_payload)

    asyncio.run(
        ig_ingest._archive_browser_capture(
            pool,
            "strava",
            "strava_streams",
            {
                "platform": "strava",
                "activity_id": "19283135496",
                "request_url": "https://www.strava.com/activities/19283135496/streams",
                "streams": {"latlng": [[1.37507, 103.750999], [1.376439, 103.75308]]},
            },
        )
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["source"] == "strava"
    assert call["artifact_id"].startswith("extension/strava_streams/19283135496/")
    assert call["target_tables"] == ["strava_activities", "strava_gps_streams"]
    assert call["metadata"]["request_url"].endswith("/19283135496/streams")
    assert call["extension"] == "json"


def test_upsert_strava_browser_stream_writes_route_tables():
    pool = _FakePool()
    pool.conn.fetchrow_result = {
        "id": "activity-uuid",
        "start_latlng": None,
        "end_latlng": None,
    }

    result = asyncio.run(
        ig_ingest._upsert_strava_browser_stream(
            pool,
            {
                "activity_id": "19283135496",
                "request_url": "https://www.strava.com/activities/19283135496/streams",
                "extension_version": "1.21.21",
                "streams": {
                    "latlng": [[1.37507, 103.750999], [1.376439, 103.75308]],
                    "time": [0, 60],
                    "altitude": [12.3, 13.4],
                },
            },
        )
    )

    assert result["stored"] == 1
    assert result["point_count"] == 2
    queries = [q for q, _ in pool.conn.executes]
    assert any("INSERT INTO strava_activities" in q for q in queries)
    assert any("INSERT INTO strava_gps_streams" in q for q in queries)
    assert any("UPDATE strava_activities" in q for q in queries)
    stream_args = next(args for q, args in pool.conn.executes if "INSERT INTO strava_gps_streams" in q)
    assert stream_args[0] == "activity-uuid"
    assert "1.37507" in stream_args[1]
    update_args = next(args for q, args in pool.conn.executes if "UPDATE strava_activities" in q)
    assert update_args[0] == "1.37507,103.750999"
    assert update_args[1] == "1.376439,103.75308"
    assert update_args[6]


def test_strava_stream_response_status_treats_empty_stream_as_processed():
    assert ig_ingest._strava_stream_response_status(
        {"stored": 0, "point_count": 0, "reason": "no_route_points"}
    ) == 200
    assert ig_ingest._strava_stream_response_status(
        {"stored": 0, "reason": "bad_activity_id"}
    ) == 400
    assert ig_ingest._strava_stream_response_status(
        {"stored": 0, "reason": "other"}
    ) == 422


def test_owner_account_for_follow_accepts_dict_and_string_owner():
    assert ig_ingest._owner_account_for_follow(
        "tiktok", "follow", {"username": "@bryanseah234"}
    ) == ("bryanseah234", "following")
    assert ig_ingest._owner_account_for_follow(
        "x", "follower", "oopspwned"
    ) == ("oopspwned", "follower")


def test_owner_account_for_follow_uses_tiktok_fallback(monkeypatch):
    monkeypatch.setattr(ig_ingest, "TIKTOK_FOLLOW_OWNER_FALLBACK", "bryanseah234")

    assert ig_ingest._owner_account_for_follow(
        "tiktok", "follow", None
    ) == ("bryanseah234", "following")
    assert ig_ingest._owner_account_for_follow(
        "instagram", "follow", None
    ) == (None, "following")
    assert ig_ingest._owner_account_for_follow(
        "tiktok", "seen", None
    ) == (None, None)


def test_archive_browser_capture_failure_records_dlq(monkeypatch):
    pool = _FakePool()

    def fake_write_raw_payload(**kwargs):
        return SimpleNamespace(ok=False, error="vault root missing")

    monkeypatch.setattr(ig_ingest, "write_raw_payload", fake_write_raw_payload)

    asyncio.run(
        ig_ingest._archive_browser_capture(
            pool,
            "instagram",
            "comments",
            {
                "platform": "instagram",
                "post_id": "post123",
                "comments": [{"platform_comment_id": "c1", "text": "hi"}],
            },
        )
    )

    assert len(pool.conn.executes) == 1
    query, args = pool.conn.executes[0]
    assert "dead_letter_queue" in query
    assert args[0] == "instagram"
    assert args[1] == "post123"
    assert args[2].startswith("extension/comments/post123/")
    assert "browser raw archive failed" in args[3]
