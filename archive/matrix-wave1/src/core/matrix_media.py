"""Wave 1 Phase 1: Matrix media downloader.

Resolves an ``mxc://`` URI to a local file on the collector drive,
decrypts the payload if it's an encrypted attachment, hashes it, and
returns the path + sha256 so the writer can persist
``media_local_path`` / ``media_sha256`` against the originating
matrix_events row.

Layout::

    <base_dir>/<room_id_safe>/<event_id_safe>.<ext>

    room_id_safe  = "!ABC:beeper.com" -> "_ABC__beeper.com"
    event_id_safe = "$xyz" -> "_xyz" (no path separators allowed)

Atomicity:
    Downloads stream to ``<final>.tmp``, are fsync'd, then ``os.replace``
    promotes them. A crash mid-download leaves only the .tmp behind —
    callers can simply retry (the row stays ``media_local_path IS NULL``).

Drive health:
    ``check_drive(base_dir)`` from ``src.core.drive_check`` gates every
    download. If Z: is detached we raise ``MatrixMediaDriveDetached``
    immediately so the caller can pause without partial writes.

Encryption:
    Encrypted attachments carry a ``file`` block on the event content
    with ``key`` (JWK), ``iv``, ``hashes.sha256``, and ``url``. We pass
    those into ``nio.crypto.attachments.decrypt_attachment``.
    Unencrypted attachments use the plain ``url`` field.

We do NOT transcode, re-encode, or extract video frames here — that's
the face_tracker / media_archival concern.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from src.core.dedupe_hash import sha256_file_async
from src.core.drive_check import check_drive

logger = logging.getLogger(__name__)


# ── public exceptions ─────────────────────────────────────────────────────


class MatrixMediaError(Exception):
    """Base class for media downloader errors."""


class MatrixMediaDriveDetached(MatrixMediaError):
    """The configured base drive is missing/unwritable; refuse to write."""


class MatrixMediaDecryptError(MatrixMediaError):
    """Decryption of an encrypted attachment failed."""


# ── constants ─────────────────────────────────────────────────────────────


DEFAULT_BASE_DIR = os.environ.get("MATRIX_MEDIA_DIR", r"Z:\matrix_media")

# Common content-type → extension mapping. Falls back to ``.bin``.
_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "text/plain": ".txt",
}


_MXC_RE = re.compile(r"^mxc://(?P<server>[^/]+)/(?P<media_id>[A-Za-z0-9_\-+]+)$")


# ── downloader ────────────────────────────────────────────────────────────


class MatrixMediaDownloader:
    """Downloads + decrypts Matrix media into the collector drive.

    Constructor parameters
    ──────────────────────
    client : object
        Anything exposing ``download(server_name, media_id) -> response``
        where ``response`` has either ``body`` (bytes) or
        ``filename`` / ``content_type`` attributes — matches matrix-nio's
        ``AsyncClient.download``. We accept a callable directly for tests
        via ``download_fn``.
    base_dir : str | Path
        Root directory for materialised files. Defaults to
        ``MATRIX_MEDIA_DIR`` env or ``Z:\\matrix_media``.
    download_fn : awaitable callable
        Optional override of the network call — useful in tests. Signature
        ``(server_name: str, media_id: str) -> response``.
    decrypt_fn : callable
        Optional override of ``nio.crypto.attachments.decrypt_attachment``.
    sha256_fn : awaitable callable
        Optional override of the file hasher (default
        ``dedupe_hash.sha256_file_async``).
    """

    def __init__(
        self,
        client: Any = None,
        base_dir: Optional[str | os.PathLike] = None,
        *,
        download_fn: Optional[Any] = None,
        decrypt_fn: Optional[Any] = None,
        sha256_fn: Optional[Any] = None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self.client = client
        self.base_dir = Path(base_dir or DEFAULT_BASE_DIR)
        self._download_fn = download_fn
        self._decrypt_fn = decrypt_fn or _import_decrypt_attachment()
        self._sha256_fn = sha256_fn or sha256_file_async
        self.log = log or logger

    # ── public API ────────────────────────────────────────────────────

    async def download(
        self,
        event_id: str,
        room_id: str,
        mxc_uri: str,
        encrypted_info: Optional[dict] = None,
        *,
        content_type: Optional[str] = None,
    ) -> tuple[Path, str]:
        """Materialise one media item to disk.

        Parameters
        ──────────
        event_id, room_id : str
            Used to derive the filesystem path.
        mxc_uri : str
            ``mxc://server/mediaid``. ValueError on malformed input.
        encrypted_info : dict | None
            For encrypted attachments, the ``file`` block from the event
            content (must contain ``key``, ``iv``, ``hashes.sha256``).
            ``None`` means the URL is plaintext.
        content_type : str | None
            Optional MIME hint (used to pick the file extension when the
            response doesn't carry one).

        Returns
        ──────
        ``(absolute_path, sha256_hex)``

        Raises
        ──────
        ``MatrixMediaDriveDetached`` if the drive is unreachable; never
        leaves a partial file.
        """
        # 1. Refuse early on a detached drive.
        if not check_drive(str(self.base_dir)):
            raise MatrixMediaDriveDetached(
                f"matrix media drive not writable: {self.base_dir}"
            )

        server_name, media_id = _parse_mxc(mxc_uri)
        ciphertext, response_mime = await self._fetch_bytes(server_name, media_id)
        mime = content_type or response_mime
        ext = _ext_for(mime)

        # 2. Decrypt if needed.
        if encrypted_info:
            try:
                payload = self._decrypt_fn(
                    ciphertext,
                    key=_extract_key(encrypted_info),
                    hash=_extract_hash(encrypted_info),
                    iv=_extract_iv(encrypted_info),
                )
            except Exception as exc:
                raise MatrixMediaDecryptError(
                    f"decrypt_attachment({event_id}) failed: {exc!r}",
                ) from exc
            if not isinstance(payload, (bytes, bytearray)):
                raise MatrixMediaDecryptError(
                    f"decrypt_attachment returned non-bytes: {type(payload)!r}",
                )
        else:
            payload = ciphertext

        # 3. Resolve target path and ensure parent exists.
        target = self._resolve_path(room_id, event_id, ext)
        target.parent.mkdir(parents=True, exist_ok=True)

        # 4. Atomic write: payload -> tmp, fsync, rename.
        tmp = target.with_suffix(target.suffix + ".tmp")
        await asyncio.to_thread(_atomic_write_bytes, tmp, target, payload)

        # 5. Hash from disk (matches the on-disk content, not in-memory bytes).
        sha = await self._sha256_fn(target)
        self.log.info(
            "Matrix media: wrote %s (%d bytes, sha256=%s)",
            target, target.stat().st_size, sha[:12],
        )
        return target, sha

    # ── network ───────────────────────────────────────────────────────

    async def _fetch_bytes(self, server_name: str, media_id: str) -> tuple[bytes, Optional[str]]:
        """Pull the raw ciphertext (or plaintext) bytes from the homeserver.

        Returns ``(bytes, content_type_or_None)``. Uses the injected
        ``download_fn`` if provided, otherwise calls
        ``client._client.download(server_name, media_id)`` (matrix-nio
        AsyncClient API).
        """
        if self._download_fn is not None:
            resp = await self._download_fn(server_name, media_id)
        else:
            nio_client = getattr(self.client, "_client", None)
            if nio_client is None or not hasattr(nio_client, "download"):
                raise MatrixMediaError(
                    "no download function configured; pass download_fn or a logged-in client",
                )
            resp = await nio_client.download(server_name, media_id)

        # Be tolerant of dict / object responses.
        body = getattr(resp, "body", None)
        if body is None and isinstance(resp, dict):
            body = resp.get("body")
        if body is None:
            raise MatrixMediaError(
                f"download response has no body for {server_name}/{media_id}: {resp!r}",
            )
        if not isinstance(body, (bytes, bytearray)):
            raise MatrixMediaError(
                f"download body is not bytes: {type(body)!r}",
            )
        ct = getattr(resp, "content_type", None) or (
            resp.get("content_type") if isinstance(resp, dict) else None
        )
        return bytes(body), ct

    # ── path helpers ──────────────────────────────────────────────────

    def _resolve_path(self, room_id: str, event_id: str, ext: str) -> Path:
        return self.base_dir / _safe_room(room_id) / f"{_safe_event(event_id)}{ext}"


# ── module-level helpers ──────────────────────────────────────────────────


def _parse_mxc(uri: str) -> tuple[str, str]:
    if not uri or not isinstance(uri, str):
        raise ValueError(f"mxc uri must be a non-empty string, got {uri!r}")
    m = _MXC_RE.match(uri.strip())
    if not m:
        raise ValueError(f"malformed mxc uri: {uri!r}")
    return m.group("server"), m.group("media_id")


_PATH_BAD = re.compile(r"[^A-Za-z0-9._\-]")


def _safe_room(room_id: str) -> str:
    """!ABC:beeper.com -> _ABC__beeper.com (no path separators)."""
    s = room_id.replace("!", "_").replace(":", "__")
    s = _PATH_BAD.sub("_", s)
    return s or "_unknown"


def _safe_event(event_id: str) -> str:
    """$xyz -> _xyz, dropping anything outside [A-Za-z0-9._-]."""
    s = event_id.replace("$", "_").replace(":", "__")
    s = _PATH_BAD.sub("_", s)
    return s or "_unknown"


def _ext_for(mime: Optional[str]) -> str:
    if not mime:
        return ".bin"
    return _EXT_BY_MIME.get(mime.lower(), ".bin")


def _extract_key(info: dict) -> str:
    """Encrypted-attachment ``key`` is a JWK dict in event content;
    nio's decrypt_attachment accepts the raw key string ``k``.

    matrix-nio's ``decrypt_attachment`` signature wants:
        key : the JWK 'k' value (base64url, no padding)

    See nio.crypto.attachments.decrypt_attachment docstring.
    """
    key = info.get("key")
    if isinstance(key, dict):
        k = key.get("k")
        if not k:
            raise MatrixMediaDecryptError("encrypted file.key has no 'k'")
        return k
    if isinstance(key, str):
        return key
    raise MatrixMediaDecryptError(f"encrypted file.key has unexpected type: {type(key)!r}")


def _extract_iv(info: dict) -> str:
    iv = info.get("iv")
    if not isinstance(iv, str) or not iv:
        raise MatrixMediaDecryptError("encrypted file.iv missing or non-string")
    return iv


def _extract_hash(info: dict) -> str:
    hashes = info.get("hashes") or {}
    h = hashes.get("sha256")
    if not isinstance(h, str) or not h:
        raise MatrixMediaDecryptError("encrypted file.hashes.sha256 missing")
    return h


def _atomic_write_bytes(tmp: Path, final: Path, payload: bytes) -> None:
    """Synchronous helper: write -> fsync -> os.replace.

    Runs in a thread (asyncio.to_thread) so the event loop isn't blocked
    on the fsync.
    """
    # Truncate any leftover .tmp from a previous crashed run.
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            # fsync isn't supported on every fs (e.g. some windows shares);
            # log-and-continue rather than fail the whole download.
            pass
    os.replace(tmp, final)


def _import_decrypt_attachment():
    """Lazy-import nio.crypto.attachments.decrypt_attachment.

    Kept lazy so the test suite can import this module without
    matrix-nio[e2e] crypto deps being importable in the worker thread
    (cryptography/libolm). Tests inject ``decrypt_fn`` explicitly.
    """
    try:
        from nio.crypto.attachments import decrypt_attachment  # type: ignore
        return decrypt_attachment
    except Exception as exc:  # pragma: no cover - import guard
        logger.debug("nio decrypt_attachment unavailable: %r", exc)
        return _missing_decrypt_attachment


def _missing_decrypt_attachment(*args, **kwargs):  # pragma: no cover
    raise MatrixMediaDecryptError(
        "matrix-nio[e2e] not available — cannot decrypt encrypted attachments",
    )


__all__ = [
    "DEFAULT_BASE_DIR",
    "MatrixMediaDecryptError",
    "MatrixMediaDownloader",
    "MatrixMediaDriveDetached",
    "MatrixMediaError",
]
