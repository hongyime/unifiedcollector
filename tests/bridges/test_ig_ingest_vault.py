import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from src.bridges import ig_ingest


class _FakeConn:
    def __init__(self):
        self.executes = []
        self.fetchvals = []
        self.fetchrow_result = None
        self.fetchval_result = None

    async def fetchval(self, query, *args):
        self.fetchvals.append((query, args))
        return self.fetchval_result

    async def fetchrow(self, query, *args):
        return self.fetchrow_result

    async def execute(self, query, *args):
        self.executes.append((query, args))
        return "INSERT 0 1"


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


class _FakeRequest(dict):
    def __init__(self, app, body):
        super().__init__()
        self.app = app
        self._body = body

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

    assert len(pool.conn.executes) == 1
    query, args = pool.conn.executes[0]
    assert "browser_ingest_events" in query
    assert args[:5] == ("threads", "media", "feed", 12, 3)
    assert "extension_version" in args[5]


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


def test_record_strava_stream_http_429_writes_rate_limit_event(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(ig_ingest, "STRAVA_BROWSER_429_COOLDOWN_SECONDS", 1234)

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
    assert len(pool.conn.executes) == 1
    query, args = pool.conn.executes[0]
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
