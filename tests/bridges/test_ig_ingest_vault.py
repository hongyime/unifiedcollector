import asyncio
from types import SimpleNamespace

from src.bridges import ig_ingest


class _FakeConn:
    def __init__(self):
        self.executes = []

    async def fetchval(self, query, *args):
        return None

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


class _NoDownloadSession:
    def get(self, *args, **kwargs):
        raise AssertionError("extension ingest should not download when vault is unavailable")


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
    assert call["metadata"]["extension_version"] == "1.21.19"
    assert pool.conn.executes == []


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
