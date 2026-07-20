import asyncio
from pathlib import Path

from src.bridges import ig_ingest
from src.core.vault import VaultHealth


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
        "vault_health",
        lambda: VaultHealth(
            root=Path(tmp_path / "vault"),
            available=False,
            writable=False,
            error="vault root missing",
        ),
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
    assert "vault unavailable before extension media write" in args[3]
