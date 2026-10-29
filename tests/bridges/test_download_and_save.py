"""SSRF/size guards for extension-supplied media downloads."""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from src.bridges import ig_ingest

ALLOWED_URL = "https://scontent.cdninstagram.com/v/t51/photo.jpg"


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


class _Content:
    def __init__(self, data: bytes, chunk_cap: int | None = None):
        self._data = data
        self._pos = 0
        self._chunk_cap = chunk_cap

    async def read(self, n=-1):
        if self._pos >= len(self._data):
            return b""
        if n is None or n < 0:
            n = len(self._data) - self._pos
        if self._chunk_cap is not None:
            n = min(n, self._chunk_cap)
        end = min(self._pos + n, len(self._data))
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk


class _Response:
    def __init__(self, data: bytes, url: str = ALLOWED_URL, status: int = 200, chunk_cap: int | None = None):
        self._data = data
        self.url = url
        self.status = status
        self.headers = {"content-type": "image/jpeg"}
        self.content = _Content(data, chunk_cap=chunk_cap)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingSession:
    def __init__(self, data: bytes = b"", url: str = ALLOWED_URL, status: int = 200, chunk_cap: int | None = None):
        self.calls = []
        self._response = _Response(data, url=url, status=status, chunk_cap=chunk_cap)

    def get(self, url, *args, **kwargs):
        self.calls.append(url)
        return self._response


def _allowlist(monkeypatch, *hosts):
    monkeypatch.setattr(ig_ingest, "BRIDGE_MEDIA_ALLOWED_HOSTS", list(hosts))


def _allow_writes(monkeypatch, tmp_path):
    artifact_path = tmp_path / "blob.jpg"
    monkeypatch.setattr(ig_ingest, "MEDIA_ROOT", str(tmp_path))
    monkeypatch.setattr(ig_ingest, "assert_media_write_allowed", lambda *a, **k: None)
    monkeypatch.setattr(
        ig_ingest,
        "_inspect_media_payload",
        lambda data, ct: (True, "jpg", "photo", None, hashlib.sha256(data).hexdigest()),
    )
    monkeypatch.setattr(
        ig_ingest,
        "_write_extension_media_artifacts_sync",
        lambda **kwargs: (
            SimpleNamespace(path=artifact_path, error=None, partial=False, ok=True),
            SimpleNamespace(enabled=False, ok=True, relative_path=None, error=None),
            "{}",
            artifact_path,
        ),
    )
    async def _consistency(*args, **kwargs):
        return SimpleNamespace(ok=True, errors=())

    monkeypatch.setattr(ig_ingest, "verify_media_item_db_consistency", _consistency)



def test_validator_rejects_non_https():
    with pytest.raises(ig_ingest.BridgeMediaRejected):
        ig_ingest._validate_media_url("http://169.254.169.254/latest/meta-data/")


def test_validator_rejects_disallowed_host(monkeypatch):
    _allowlist(monkeypatch, "*.cdninstagram.com")
    with pytest.raises(ig_ingest.BridgeMediaRejected):
        ig_ingest._validate_media_url("https://evil.example.com/x.jpg")


def test_validator_accepts_wildcard_and_exact(monkeypatch):
    _allowlist(monkeypatch, "*.cdninstagram.com", "pbs.twimg.com")
    ig_ingest._validate_media_url("https://scontent-fra3-1.cdninstagram.com/x.jpg")
    ig_ingest._validate_media_url("https://pbs.twimg.com/media/x.jpg")


def test_download_allowed_https_host_succeeds(monkeypatch, tmp_path):
    from src.notifications import realtime_feed

    monkeypatch.setattr(realtime_feed, "enqueue_from_insert", lambda **kwargs: {"status": "enqueued"})
    _allowlist(monkeypatch, "*.cdninstagram.com")
    _allow_writes(monkeypatch, tmp_path)
    session = _RecordingSession(data=b"jpegbytes")

    saved = asyncio.run(
        ig_ingest._download_and_save(
            _FakePool(), session, "instagram", "alice",
            {"content_id": "abc123", "url": ALLOWED_URL},
        )
    )

    assert saved is True
    assert session.calls == [ALLOWED_URL]


def test_download_rejects_http_before_request(monkeypatch, tmp_path):
    _allowlist(monkeypatch, "*.cdninstagram.com")
    _allow_writes(monkeypatch, tmp_path)
    session = _RecordingSession()

    stats = {}
    saved = asyncio.run(
        ig_ingest._download_and_save(
            _FakePool(), session, "instagram", "alice",
            {"content_id": "abc123", "url": "http://169.254.169.254/latest/meta-data/"},
            stats,
        )
    )

    assert saved is False
    assert session.calls == []
    assert stats["blocked_url"] == 1


def test_download_rejects_disallowed_host(monkeypatch, tmp_path):
    _allowlist(monkeypatch, "*.cdninstagram.com")
    _allow_writes(monkeypatch, tmp_path)
    session = _RecordingSession()

    stats = {}
    saved = asyncio.run(
        ig_ingest._download_and_save(
            _FakePool(), session, "instagram", "alice",
            {"content_id": "abc123", "url": "https://evil.example.com/x.jpg"},
            stats,
        )
    )

    assert saved is False
    assert session.calls == []
    assert stats["blocked_url"] == 1


def test_download_reads_full_multi_chunk_body(monkeypatch, tmp_path):
    from src.notifications import realtime_feed

    monkeypatch.setattr(realtime_feed, "enqueue_from_insert", lambda **kwargs: {"status": "enqueued"})
    _allowlist(monkeypatch, "*.cdninstagram.com")
    _allow_writes(monkeypatch, tmp_path)
    body = b"a" * 5000
    captured = []
    monkeypatch.setattr(
        ig_ingest,
        "_inspect_media_payload",
        lambda data, ct: (
            captured.append(len(data)) or True,
            "jpg",
            "photo",
            None,
            hashlib.sha256(data).hexdigest(),
        ),
    )
    session = _RecordingSession(data=body, chunk_cap=1024)

    saved = asyncio.run(
        ig_ingest._download_and_save(
            _FakePool(), session, "instagram", "alice",
            {"content_id": "abc123", "url": ALLOWED_URL},
        )
    )

    assert saved is True
    assert captured == [5000]


def test_download_rejects_oversized_body_and_writes_nothing(monkeypatch, tmp_path):
    _allowlist(monkeypatch, "*.cdninstagram.com")
    _allow_writes(monkeypatch, tmp_path)
    monkeypatch.setattr(ig_ingest, "BRIDGE_MEDIA_MAX_BYTES", 4096)
    session = _RecordingSession(data=b"x" * 5000, chunk_cap=1024)
    written = []
    monkeypatch.setattr(
        ig_ingest,
        "_write_extension_media_artifacts_sync",
        lambda **kwargs: written.append(kwargs) or (_ for _ in ()).throw(AssertionError("must not persist")),
    )

    stats = {}
    saved = asyncio.run(
        ig_ingest._download_and_save(
            _FakePool(), session, "instagram", "alice",
            {"content_id": "abc123", "url": ALLOWED_URL},
            stats,
        )
    )

    assert saved is False
    assert stats["media_too_large"] == 1
    assert written == []
    assert list(tmp_path.iterdir()) == []
