"""Unit tests for src/core/matrix_media.py.

Pure-unit. The matrix-nio download() and decrypt_attachment helpers are
both injected via the constructor — there's no live homeserver, no
httpx, no real Z: drive in scope.

We DO write to the real filesystem under tmp_path so the atomic-write
+ sha256 verification paths exercise actual disk I/O.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.matrix_media import (
    MatrixMediaDecryptError,
    MatrixMediaDownloader,
    MatrixMediaDriveDetached,
    MatrixMediaError,
    _ext_for,
    _parse_mxc,
    _safe_event,
    _safe_room,
)


# ── helpers ───────────────────────────────────────────────────────────────


class StubDownloadResp:
    def __init__(self, body: bytes, content_type: str = "image/png"):
        self.body = body
        self.content_type = content_type


def _make_downloader(tmp_path, *, payload=b"hello", content_type="image/png", decrypt=None):
    """Build a downloader wired to in-memory fakes."""
    download_fn = AsyncMock(
        return_value=StubDownloadResp(payload, content_type=content_type)
    )

    return (
        MatrixMediaDownloader(
            base_dir=tmp_path,
            download_fn=download_fn,
            decrypt_fn=decrypt or (lambda c, key, hash, iv: c),  # default identity
        ),
        download_fn,
    )


# ── pure helpers ──────────────────────────────────────────────────────────


def test_parse_mxc_valid():
    server, mid = _parse_mxc("mxc://beeper.com/abcDEF123_-")
    assert server == "beeper.com"
    assert mid == "abcDEF123_-"


@pytest.mark.parametrize("bad", ["", None, "http://x/y", "mxc://server", "mxc:///id"])
def test_parse_mxc_rejects_bad(bad):
    with pytest.raises(ValueError):
        _parse_mxc(bad)


def test_safe_room_strips_special_chars():
    out = _safe_room("!ABC:beeper.com")
    assert out == "_ABC__beeper.com"
    assert "/" not in out and ":" not in out


def test_safe_event_strips_dollar():
    out = _safe_event("$xyzABC")
    assert out.startswith("_")
    assert "$" not in out


def test_ext_for_known_mime():
    assert _ext_for("image/png") == ".png"
    assert _ext_for("video/mp4") == ".mp4"
    assert _ext_for("IMAGE/JPEG") == ".jpg"


def test_ext_for_unknown_falls_back():
    assert _ext_for("application/x-weird") == ".bin"
    assert _ext_for(None) == ".bin"
    assert _ext_for("") == ".bin"


# ── path layout ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_path_layout(tmp_path):
    dl, _ = _make_downloader(tmp_path)
    path, sha = await dl.download(
        event_id="$abc",
        room_id="!XYZ:beeper.com",
        mxc_uri="mxc://beeper.com/mediaABC",
    )
    # <tmp>/_XYZ__beeper.com/_abc.png
    assert path.parent.name == "_XYZ__beeper.com"
    assert path.name == "_abc.png"
    assert path.exists()


# ── unencrypted happy path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_unencrypted(tmp_path):
    payload = b"some-png-bytes" * 64
    expected_sha = hashlib.sha256(payload).hexdigest()
    dl, download_fn = _make_downloader(tmp_path, payload=payload)

    path, sha = await dl.download(
        event_id="$1",
        room_id="!r:s",
        mxc_uri="mxc://beeper.com/AAA",
    )
    assert path.read_bytes() == payload
    assert sha == expected_sha
    download_fn.assert_awaited_once_with("beeper.com", "AAA")


# ── encrypted path ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_encrypted_calls_decrypt(tmp_path):
    ciphertext = b"CIPHER" * 16
    plaintext = b"PLAIN" * 16
    expected_sha = hashlib.sha256(plaintext).hexdigest()

    decrypted_calls = []

    def fake_decrypt(c, *, key, hash, iv):
        decrypted_calls.append({"key": key, "hash": hash, "iv": iv, "len": len(c)})
        assert c == ciphertext
        return plaintext

    dl, _ = _make_downloader(tmp_path, payload=ciphertext, decrypt=fake_decrypt)

    encrypted_info = {
        "url": "mxc://beeper.com/AAA",
        "key": {"k": "JWK-K-VALUE"},
        "iv": "IV-B64",
        "hashes": {"sha256": "HASH-B64"},
    }
    path, sha = await dl.download(
        event_id="$1",
        room_id="!r:s",
        mxc_uri="mxc://beeper.com/AAA",
        encrypted_info=encrypted_info,
    )
    assert path.read_bytes() == plaintext
    assert sha == expected_sha
    assert decrypted_calls == [
        {"key": "JWK-K-VALUE", "hash": "HASH-B64", "iv": "IV-B64", "len": len(ciphertext)},
    ]


@pytest.mark.asyncio
async def test_download_encrypted_decrypt_failure_raises(tmp_path):
    def boom(*a, **kw):
        raise RuntimeError("aes failed")

    dl, _ = _make_downloader(tmp_path, payload=b"x", decrypt=boom)
    info = {"key": {"k": "k"}, "iv": "iv", "hashes": {"sha256": "h"}}
    with pytest.raises(MatrixMediaDecryptError):
        await dl.download(
            event_id="$1",
            room_id="!r:s",
            mxc_uri="mxc://beeper.com/AAA",
            encrypted_info=info,
        )


@pytest.mark.asyncio
async def test_download_encrypted_missing_iv(tmp_path):
    dl, _ = _make_downloader(tmp_path, payload=b"x")
    bad_info = {"key": {"k": "k"}, "hashes": {"sha256": "h"}}  # no iv
    with pytest.raises(MatrixMediaDecryptError):
        await dl.download(
            event_id="$1",
            room_id="!r:s",
            mxc_uri="mxc://beeper.com/AAA",
            encrypted_info=bad_info,
        )


# ── atomic write ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_no_tmp_left_behind(tmp_path):
    dl, _ = _make_downloader(tmp_path, payload=b"hi")
    path, _ = await dl.download(
        event_id="$1", room_id="!r:s", mxc_uri="mxc://beeper.com/A",
    )
    # No .tmp sibling.
    siblings = list(path.parent.iterdir())
    assert len(siblings) == 1
    assert siblings[0].suffix != ".tmp"


# ── drive detach ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_drive_detached_raises_immediately(tmp_path):
    """Pointing base_dir at a non-existent path -> immediate raise; no file."""
    missing = tmp_path / "definitely-not-there"
    download_fn = AsyncMock(return_value=StubDownloadResp(b"x"))
    dl = MatrixMediaDownloader(
        base_dir=missing,
        download_fn=download_fn,
        decrypt_fn=lambda *a, **kw: b"",
    )
    with pytest.raises(MatrixMediaDriveDetached):
        await dl.download(
            event_id="$1", room_id="!r:s", mxc_uri="mxc://beeper.com/A",
        )
    # download_fn must NOT have been called — fail fast.
    download_fn.assert_not_awaited()
    # Nothing materialised.
    assert not missing.exists()


# ── error responses ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_no_body_raises(tmp_path):
    bad_resp = MagicMock(spec=[])  # no body, no content_type
    download_fn = AsyncMock(return_value=bad_resp)
    dl = MatrixMediaDownloader(
        base_dir=tmp_path,
        download_fn=download_fn,
        decrypt_fn=lambda *a, **kw: b"",
    )
    with pytest.raises(MatrixMediaError):
        await dl.download(
            event_id="$1", room_id="!r:s", mxc_uri="mxc://beeper.com/A",
        )


@pytest.mark.asyncio
async def test_download_dict_response_supported(tmp_path):
    """Some test fixtures return plain dicts; we should handle both."""
    download_fn = AsyncMock(return_value={"body": b"abc", "content_type": "image/png"})
    dl = MatrixMediaDownloader(
        base_dir=tmp_path,
        download_fn=download_fn,
        decrypt_fn=lambda *a, **kw: b"",
    )
    path, sha = await dl.download(
        event_id="$1", room_id="!r:s", mxc_uri="mxc://beeper.com/A",
    )
    assert path.read_bytes() == b"abc"
    assert sha == hashlib.sha256(b"abc").hexdigest()


# ── content type override ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_content_type_override(tmp_path):
    """Caller-supplied content_type wins over the response's."""
    dl, _ = _make_downloader(tmp_path, payload=b"v", content_type="application/octet-stream")
    path, _ = await dl.download(
        event_id="$1",
        room_id="!r:s",
        mxc_uri="mxc://beeper.com/A",
        content_type="video/mp4",
    )
    assert path.suffix == ".mp4"


# ── module exports ────────────────────────────────────────────────────────


def test_module_exports():
    from src.core import matrix_media as m
    for name in (
        "MatrixMediaDownloader",
        "MatrixMediaError",
        "MatrixMediaDecryptError",
        "MatrixMediaDriveDetached",
        "DEFAULT_BASE_DIR",
    ):
        assert name in m.__all__
