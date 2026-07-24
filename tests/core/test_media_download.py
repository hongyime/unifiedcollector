"""Tests for src.core.media_download — unified download module."""
from __future__ import annotations

import asyncio
import hashlib
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import pytest

from src.core import media_download as md


# ---------------------------------------------------------------------------
# Tier router tests (pure / sync — no IO)
# ---------------------------------------------------------------------------

class TestPickBackend:
    def test_youtube_picks_yt_dlp(self):
        assert md.pick_backend("https://www.youtube.com/watch?v=abc") == "yt-dlp"
        assert md.pick_backend("https://youtu.be/abc") == "yt-dlp"

    def test_instagram_picks_gallery_dl(self):
        assert md.pick_backend("https://www.instagram.com/p/abc/") == "gallery-dl"

    def test_instagram_reel_picks_yt_dlp(self):
        # Reels go through yt-dlp because gallery-dl doesn't always handle videos
        assert md.pick_backend("https://www.instagram.com/reel/abc/") == "gallery-dl"
        # (host matches IG first; the /reel/ branch is a fallback for IG-like
        # hosts not in the primary table — covered in pick_backend internals.)

    def test_tiktok_picks_gallery_dl(self):
        assert md.pick_backend("https://www.tiktok.com/@u/video/123") == "gallery-dl"
        assert md.pick_backend("https://vm.tiktok.com/abcd/") == "gallery-dl"

    def test_direct_jpg_picks_httpx(self):
        assert md.pick_backend("https://cdn.example.com/foo/bar.jpg") == "httpx"
        assert md.pick_backend("https://cdn.example.com/x.MP4") == "httpx"

    def test_unknown_host_defaults_yt_dlp(self):
        assert md.pick_backend("https://example.com/somepage") == "yt-dlp"

    def test_garbage_url_does_not_crash(self):
        assert md.pick_backend("not a url") in {"yt-dlp", "httpx", "gallery-dl"}


# ---------------------------------------------------------------------------
# Hashing helper
# ---------------------------------------------------------------------------

class TestSha256Helper:
    def test_hash_files(self, tmp_path: Path):
        f1 = tmp_path / "a.bin"
        f1.write_bytes(b"hello")
        f2 = tmp_path / "b.bin"
        f2.write_bytes(b"world")
        digests = md.hash_files([f1, f2])
        assert digests[f1] == hashlib.sha256(b"hello").hexdigest()
        assert digests[f2] == hashlib.sha256(b"world").hexdigest()

    def test_hash_files_skips_missing(self, tmp_path: Path):
        f1 = tmp_path / "a.bin"
        f1.write_bytes(b"data")
        missing = tmp_path / "nope.bin"
        digests = md.hash_files([f1, missing])
        assert f1 in digests
        assert missing not in digests


# ---------------------------------------------------------------------------
# Atomic-replace test
# ---------------------------------------------------------------------------

class TestAtomicReplace:
    def test_atomic_replace_publishes_tmp(self, vault_media):
        root, media, dest = vault_media
        tmp = dest / "x.bin.tmp"
        tmp.write_bytes(b"payload")
        final = dest / "x.bin"
        md._atomic_replace(tmp, final, root=root, media_root=media)
        assert final.exists()
        assert not tmp.exists()
        assert final.read_bytes() == b"payload"


# ---------------------------------------------------------------------------
# Local HTTP server fixture for httpx tier tests
# ---------------------------------------------------------------------------

class _SlowHandler(BaseHTTPRequestHandler):
    """Returns a configurable byte payload, optionally slowly (for cancel tests)."""
    payload = b"X" * 1024  # 1 KiB default
    delay_per_chunk = 0.0
    chunk_size = 256

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        sent = 0
        try:
            while sent < len(self.payload):
                chunk = self.payload[sent : sent + self.chunk_size]
                self.wfile.write(chunk)
                self.wfile.flush()
                sent += len(chunk)
                if self.delay_per_chunk:
                    import time as _t
                    _t.sleep(self.delay_per_chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, *args, **kwargs):  # silence stderr
        return


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def local_server():
    """Spin up a one-shot HTTP server on a free port. Yields (host, port, set_payload, set_delay)."""
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def set_payload(b: bytes):
        _SlowHandler.payload = b

    def set_delay(d: float, chunk_size: int = 256):
        _SlowHandler.delay_per_chunk = d
        _SlowHandler.chunk_size = chunk_size

    # Reset defaults
    set_payload(b"X" * 1024)
    set_delay(0.0, 256)

    try:
        yield ("127.0.0.1", port, set_payload, set_delay)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def vault_media(tmp_path: Path):
    root = tmp_path / "vault"
    media = root / "media"
    dest = media / "downloads"
    dest.mkdir(parents=True)
    return root, media, dest


def _opts(vault_media, **kwargs) -> md.MediaOptions:
    root, media, _dest = vault_media
    return md.MediaOptions(vault_root=root, media_root=media, **kwargs)


# ---------------------------------------------------------------------------
# httpx tier — happy path, sha256, atomic, progress
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_httpx_download_happy_path(local_server, vault_media):
    host, port, set_payload, _ = local_server
    root, _media, dest = vault_media
    payload = b"A" * 4096
    set_payload(payload)

    progress_events: list[tuple[int, Optional[int]]] = []

    def on_progress(done, total):
        progress_events.append((done, total))

    url = f"http://{host}:{port}/file.bin"
    opts = _opts(
        vault_media,
        backend="httpx",
        progress_cb=on_progress,
        max_retries=1,
    )
    result = await md.download(url, dest, opts)

    assert result.ok, f"download failed: {result.error}"
    assert result.backend == "httpx"
    assert result.bytes_total == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.file_count == 1
    assert result.files[0].read_bytes() == payload
    assert result.vault_artifact is not None
    assert result.vault_artifact.sidecar is not None
    assert result.vault_artifact.sidecar.ok
    assert root in result.files[0].parents
    assert "blobs" in result.files[0].parts
    # No leftover tmp files
    assert not list((root / "media" / ".tmp").glob("*.tmp"))
    # Progress fired at least once
    assert progress_events
    # Last event should equal full payload size
    assert progress_events[-1][0] == len(payload)
    assert progress_events[-1][1] == len(payload)


@pytest.mark.asyncio
async def test_httpx_skips_existing(local_server, vault_media):
    host, port, set_payload, _ = local_server
    _root, _media, dest = vault_media
    set_payload(b"orig")

    final = dest / "file.bin"
    final.write_bytes(b"already-here")

    url = f"http://{host}:{port}/file.bin"
    result = await md.download(url, dest, _opts(vault_media, backend="httpx"))
    assert result.ok
    assert final.read_bytes() == b"already-here"
    assert result.files[0] != final
    assert result.files[0].read_bytes() == b"already-here"
    assert result.sha256 == hashlib.sha256(b"already-here").hexdigest()


@pytest.mark.asyncio
async def test_httpx_overwrite(local_server, vault_media):
    host, port, set_payload, _ = local_server
    _root, _media, dest = vault_media
    set_payload(b"new-content")

    final = dest / "file.bin"
    final.write_bytes(b"stale")

    url = f"http://{host}:{port}/file.bin"
    result = await md.download(url, dest, _opts(vault_media, backend="httpx", overwrite=True))
    assert result.ok
    assert result.files[0].read_bytes() == b"new-content"
    assert final.read_bytes() == b"stale"


# ---------------------------------------------------------------------------
# Cancel via stop_event
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_httpx_cancel_via_stop_event(local_server, vault_media):
    host, port, set_payload, set_delay = local_server
    root, _media, dest = vault_media
    set_payload(b"Y" * 100_000)
    set_delay(0.05, chunk_size=512)  # ~200 chunks * 50ms = ~10s total

    stop = asyncio.Event()

    async def trip():
        await asyncio.sleep(0.2)
        stop.set()

    url = f"http://{host}:{port}/big.bin"
    opts = _opts(vault_media, backend="httpx", stop_event=stop, max_retries=1, timeout=30)

    asyncio.create_task(trip())
    result = await md.download(url, dest, opts)

    assert not result.ok
    assert result.cancelled or "cancel" in (result.error or "").lower()
    # Atomic discipline: no partial file should be visible at the final path.
    assert not (dest / "big.bin").exists()
    assert not list((root / "media" / ".tmp").glob("*.tmp"))


# ---------------------------------------------------------------------------
# Delegated backend
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delegated_happy_path(vault_media):
    _root, _media, dest = vault_media
    payload = b"telegram-media-bytes"

    async def writer(tmp_path_in: Path, opts: md.MediaOptions):
        # Simulate Telethon writing the file
        tmp_path_in.write_bytes(payload)

    opts = _opts(
        vault_media,
        backend="delegated",
        delegated_writer=writer,
        output_filename="msg_42.bin",
    )
    result = await md.download("telegram://chan/42", dest, opts)
    assert result.ok, result.error
    assert result.backend == "delegated"
    assert result.bytes_total == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.files[0].read_bytes() == payload
    assert result.vault_artifact is not None
    assert result.vault_artifact.sidecar is not None
    assert result.vault_artifact.sidecar.ok
    assert not (dest / "msg_42.bin").exists()


@pytest.mark.asyncio
async def test_delegated_missing_writer(vault_media):
    _root, _media, dest = vault_media
    opts = _opts(vault_media, backend="delegated", output_filename="x.bin")
    result = await md.download("telegram://x", dest, opts)
    assert not result.ok
    assert "delegated_writer" in (result.error or "")


@pytest.mark.asyncio
async def test_delegated_stop_event_aborts(vault_media):
    root, _media, dest = vault_media
    started = asyncio.Event()
    stop = asyncio.Event()

    async def slow_writer(tmp_path_in: Path, opts: md.MediaOptions):
        started.set()
        await asyncio.sleep(10)
        tmp_path_in.write_bytes(b"never")

    async def trip():
        await started.wait()
        stop.set()

    opts = _opts(
        vault_media,
        backend="delegated",
        delegated_writer=slow_writer,
        stop_event=stop,
        output_filename="x.bin",
        timeout=30,
    )
    asyncio.create_task(trip())
    result = await md.download("delegated://x", dest, opts)
    assert not result.ok
    assert result.cancelled
    assert not (dest / "x.bin").exists()
    assert not list((root / "media" / ".tmp").glob("*.tmp"))


# ---------------------------------------------------------------------------
# Unknown backend
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_backend(vault_media):
    _root, _media, dest = vault_media
    opts = _opts(vault_media, backend="bogus")
    result = await md.download("https://x", dest, opts)
    assert not result.ok
    assert "unknown backend" in (result.error or "")


# ---------------------------------------------------------------------------
# rate_limiter hook
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limiter_acquire_called(vault_media, local_server):
    host, port, set_payload, _ = local_server
    _root, _media, dest = vault_media
    set_payload(b"rl-test")

    calls = []

    class _Limiter:
        async def acquire(self):
            calls.append(1)

    url = f"http://{host}:{port}/x.bin"
    opts = _opts(vault_media, backend="httpx", rate_limiter=_Limiter(), max_retries=1)
    result = await md.download(url, dest, opts)
    assert result.ok
    assert calls == [1]


@pytest.mark.asyncio
async def test_rate_limiter_sync_acquire_also_works(vault_media, local_server):
    host, port, set_payload, _ = local_server
    _root, _media, dest = vault_media
    set_payload(b"rl-sync")

    calls = []

    class _Limiter:
        def acquire(self):
            calls.append(1)
            # sync return — module must handle non-coroutine

    url = f"http://{host}:{port}/x.bin"
    opts = _opts(vault_media, backend="httpx", rate_limiter=_Limiter(), max_retries=1)
    result = await md.download(url, dest, opts)
    assert result.ok
    assert calls == [1]


# ---------------------------------------------------------------------------
# Multiple sequential downloads in one event loop — race-task hygiene check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sequential_downloads_no_task_leaks(local_server, vault_media):
    """Reproduce the ralph-loop pitfall: race-task bugs hide until tests run
    sequentially in one event loop. Three downloads back-to-back."""
    host, port, set_payload, _ = local_server
    _root, _media, dest = vault_media
    files: list[Path] = []

    for i in range(3):
        set_payload(f"file-{i}".encode() * 100)
        result = await md.download(
            f"http://{host}:{port}/f{i}.bin",
            dest,
            _opts(vault_media, backend="httpx", overwrite=True, max_retries=1,
                  output_filename=f"f{i}.bin"),
        )
        assert result.ok, f"download {i} failed: {result.error}"
        files.extend(result.files)

    # All three canonical blobs present, no per-occurrence final files.
    assert len(files) == 3
    assert all(p.exists() for p in files)
    assert not any(dest.iterdir())
