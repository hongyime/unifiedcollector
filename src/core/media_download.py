"""Unified media download — single canonical entry point for 7 collectors.

Replaces ~3000 LOC of duplicated download logic spread across:
  github / instagram / lemon8 / strava / telegram / tiktok / whatsapp.

Design
------
* Async-first: ``await download(url, dest_dir, options)`` returns ``MediaResult``.
* Tier router: chooses the right backend per URL/source.
    - gallery-dl  : Instagram / TikTok / Lemon8 / Twitter (galleries, batches)
    - yt-dlp      : YouTube + Instagram reels + any video URL
    - httpx       : direct media URLs (CDN, raw .jpg/.mp4 endpoints)
    - delegated   : Telethon / Baileys do their own media download via
                    library APIs — caller passes ``backend="delegated"``
                    and supplies a coroutine that writes a file. We still
                    handle atomic-rename + sha256 + cancel discipline.
* Progress callback: ``progress_cb(done_bytes, total_bytes)`` (httpx tier only;
  subprocess tiers stream a *line-text* progress hook via ``log_cb`` because
  yt-dlp/gallery-dl don't expose precise bytes through stdout reliably).
* Cooperative cancel: pass ``stop_event: asyncio.Event``. We race
  proc / timeout / stop using ``asyncio.wait(FIRST_COMPLETED)`` and cancel
  the losers in ``finally``  — see ralph-loop ``Async race-task bugs hide
  until tests run sequentially in one event loop`` pitfall.
* Atomic write: download to ``<final>.tmp``, fsync, ``os.replace`` to final.
* SHA-256 of completed file is part of every ``MediaResult``.
* Retry: thin wrapper over ``src.core.resilience.async_retry`` exponential
  backoff with jitter.

Out of scope (do NOT add)
-------------------------
* Upload / publish.
* Transcode / re-encode / thumbnail generation.
* Cloud storage destinations — Z: drive only.

Hooks for downstream Wave-0 agents
----------------------------------
* tor_proxy: ``MediaOptions.tor_proxy_url`` is plumbed through to httpx
  and to subprocess argv (env-var ``ALL_PROXY`` for yt-dlp/gallery-dl).
  Currently no-op unless caller sets it. Agent C of Batch 2 will wire
  ``src/core/tor_proxy.py`` → fill this field.
* adaptive_rate: ``MediaOptions.rate_limiter`` accepts an awaitable
  ``acquire()`` callable invoked before each network attempt. No-op
  when None. Agent C parallel here will wire ``src/core/adaptive_rate.py``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence
from urllib.parse import urlparse

from . import subprocess_downloader as _sub
from .resilience import async_retry
from .vault import (
    VAULT_ROOT,
    AtomicArtifactResult,
    assert_media_write_allowed,
    ensure_vault_available,
    write_atomic_artifact_from_path,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ProgressCB = Callable[[int, Optional[int]], None]
"""Bytes-progress callback: (downloaded, total_or_None)."""

LogCB = Callable[[str], None]
"""Line-oriented log callback for subprocess tiers."""


@dataclass
class MediaOptions:
    """Per-call download options. All fields optional — sane defaults."""
    # Core
    backend: str = "auto"            # "auto" | "gallery-dl" | "yt-dlp" | "httpx" | "delegated"
    timeout: float = 300.0
    cookies_file: Optional[str] = None
    extra_args: Optional[Sequence[str]] = None
    output_filename: Optional[str] = None  # if set, used as the occurrence basename (httpx/delegated)
    source: str = "media_download"
    artifact_kind: str = "media_blob"
    artifact_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    vault_root: Optional[Path] = None
    media_root: Optional[Path] = None

    # Behaviour
    overwrite: bool = False
    max_retries: int = 3
    retry_base_delay: float = 1.0

    # Hooks (downstream agents fill these)
    tor_proxy_url: Optional[str] = None
    rate_limiter: Optional[Any] = None  # object with async acquire()

    # Callbacks
    progress_cb: Optional[ProgressCB] = None
    log_cb: Optional[LogCB] = None
    stop_event: Optional[asyncio.Event] = None

    # Delegated backend
    delegated_writer: Optional[Callable[[Path, "MediaOptions"], Awaitable[None]]] = None
    """For backend='delegated': coroutine that writes the file at the given
    path. We provide a .tmp path; on success we rename to final and hash."""


@dataclass
class MediaResult:
    """Outcome of one download() call."""
    ok: bool
    url: str
    backend: str
    files: list[Path] = field(default_factory=list)
    bytes_total: int = 0
    sha256: Optional[str] = None     # only for single-file tiers (httpx/delegated)
    elapsed: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    error: Optional[str] = None
    # Raw subprocess result (for collectors that want stderr tail / metadata)
    subprocess_result: Optional[_sub.DownloadResult] = None
    # Canonical vault commit for single-file tiers (httpx/delegated).
    vault_artifact: Optional[AtomicArtifactResult] = None

    @property
    def file_count(self) -> int:
        return len(self.files)


# ---------------------------------------------------------------------------
# Tier router
# ---------------------------------------------------------------------------

_GALLERY_DL_HOSTS = {
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
    "lemon8-app.com", "www.lemon8-app.com",
    "twitter.com", "www.twitter.com", "x.com", "www.x.com",
    "pinterest.com", "www.pinterest.com",
}

_YT_DLP_HOSTS = {
    "youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com",
    "vimeo.com", "www.vimeo.com",
    "twitch.tv", "www.twitch.tv",
}

_DIRECT_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv",
    ".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac",
    ".pdf", ".zip", ".bin",
}


def pick_backend(url: str) -> str:
    """Heuristic tier-router. Returns one of:
    'gallery-dl' | 'yt-dlp' | 'httpx'.

    Caller can override via ``MediaOptions.backend``. Logic:
      1. Direct media URLs (URL path ends in known media extension) → httpx.
      2. Hostname in gallery-dl host set → gallery-dl.
      3. Hostname in yt-dlp host set → yt-dlp.
      4. Default → yt-dlp (broadest coverage, will fall back internally).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "yt-dlp"

    path_lower = (parsed.path or "").lower()
    # Strip query before extension check
    for ext in _DIRECT_EXTS:
        if path_lower.endswith(ext):
            return "httpx"

    host = (parsed.hostname or "").lower()
    if host in _GALLERY_DL_HOSTS:
        return "gallery-dl"
    if host in _YT_DLP_HOSTS:
        return "yt-dlp"
    # Instagram reels → yt-dlp handles videos better than gallery-dl
    if "instagram.com" in host and "/reel/" in path_lower:
        return "yt-dlp"
    return "yt-dlp"


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------

def _sha256_of_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _atomic_replace(
    tmp_path: Path,
    final_path: Path,
    *,
    root: Path = VAULT_ROOT,
    media_root: str | os.PathLike[str] | None = None,
) -> None:
    """fsync the tmp file (best-effort on Windows), then os.replace to the
    final destination.

    Both paths must be on the same filesystem (caller's responsibility — we
    write the .tmp next to the final path to guarantee that).

    Windows note: fsync on a read-only handle returns EBADF in some Python
    builds. We swallow OSError because os.replace itself is atomic and
    Windows flushes buffered writes when the writer's handle is closed.
    The fsync here is a best-effort durability barrier for POSIX hosts.
    """
    try:
        fd = os.open(str(tmp_path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # Windows / odd FS — proceed; replace is still atomic
        if os.name != "nt":
            logger.debug("fsync failed for %s", tmp_path, exc_info=True)
    assert_media_write_allowed(final_path, root=root, media_root=media_root)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(tmp_path), str(final_path))


def _option_vault_root(opts: MediaOptions) -> Path:
    return Path(opts.vault_root).resolve(strict=False) if opts.vault_root is not None else VAULT_ROOT


def _option_media_root(opts: MediaOptions) -> Path | None:
    if opts.media_root is None:
        return None
    return Path(opts.media_root).resolve(strict=False)


def _artifact_id_for(url: str, backend: str, filename: str, opts: MediaOptions) -> str:
    if opts.artifact_id:
        return opts.artifact_id
    digest = hashlib.sha256(f"{backend}:{url}:{filename}".encode("utf-8")).hexdigest()[:16]
    return f"{backend}/{digest}/{filename}"


def _download_metadata(
    url: str,
    backend: str,
    filename: str,
    dest_dir: Path,
    opts: MediaOptions,
) -> dict[str, Any]:
    return {
        **dict(opts.metadata or {}),
        "ingest_path": "media_download",
        "backend": backend,
        "source_url": url,
        "request_url": url,
        "filename": filename,
        "legacy_dest_dir": str(dest_dir),
    }


def _open_download_temp(root: Path, filename: str, backend: str) -> tuple[int, Path]:
    ensure_vault_available(root)
    tmp_dir = root / "media" / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix or ".bin"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".media_download.{backend}.",
        suffix=f"{suffix}.tmp",
        dir=str(tmp_dir),
    )
    return fd, Path(tmp_name)


def _commit_vault_artifact(
    *,
    url: str,
    backend: str,
    filename: str,
    source_path: Path,
    dest_dir: Path,
    opts: MediaOptions,
    expected_sha256: str | None = None,
    delete_source: bool,
) -> AtomicArtifactResult:
    root = _option_vault_root(opts)
    return write_atomic_artifact_from_path(
        source=opts.source,
        artifact_id=_artifact_id_for(url, backend, filename, opts),
        artifact_kind=opts.artifact_kind,
        source_path=source_path,
        extension=Path(filename).suffix,
        metadata=_download_metadata(url, backend, filename, dest_dir, opts),
        expected_sha256=expected_sha256,
        root=root,
        delete_source=delete_source,
    )


def _artifact_result_to_media_result(
    artifact: AtomicArtifactResult,
    *,
    url: str,
    backend: str,
    elapsed: float,
    fallback_bytes_total: int = 0,
    fallback_sha256: str | None = None,
) -> MediaResult:
    files = [artifact.path] if artifact.path else []
    return MediaResult(
        ok=artifact.ok,
        url=url,
        backend=backend,
        files=files,
        bytes_total=int(artifact.file_size or fallback_bytes_total or 0),
        sha256=artifact.sha256 or fallback_sha256,
        elapsed=elapsed,
        error=None if artifact.ok else artifact.error,
        vault_artifact=artifact,
    )


# ---------------------------------------------------------------------------
# httpx tier — direct URL download with progress + atomic + cancel
# ---------------------------------------------------------------------------

async def _httpx_download_one(
    url: str,
    tmp_path: Path,
    tmp_fd: int,
    opts: MediaOptions,
) -> tuple[int, str]:
    """Download a single URL to a vault temp file and return size + sha256.

    Returns (bytes_total, sha256_hex). Raises on error.
    """
    import httpx  # local import — keeps module import-clean if httpx missing

    proxies = opts.tor_proxy_url or None
    client_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(opts.timeout, connect=30.0),
        "follow_redirects": True,
    }
    if proxies:
        # httpx 0.27+: `proxy=` (not `proxies=`)
        client_kwargs["proxy"] = proxies

    bytes_total = 0
    h = hashlib.sha256()

    tmp_handle = None
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_length: Optional[int] = None
                cl = resp.headers.get("content-length")
                if cl and cl.isdigit():
                    content_length = int(cl)

                # Race the byte-stream against stop_event.
                stop_task: Optional[asyncio.Task] = None
                if opts.stop_event is not None:
                    stop_task = asyncio.create_task(opts.stop_event.wait())

                try:
                    tmp_handle = os.fdopen(tmp_fd, "wb")
                    tmp_fd = -1
                    aiter = resp.aiter_bytes(chunk_size=64 * 1024).__aiter__()
                    while True:
                        next_task = asyncio.create_task(aiter.__anext__())
                        waitset: set[asyncio.Task] = {next_task}
                        if stop_task is not None:
                            waitset.add(stop_task)

                        done, _pending = await asyncio.wait(
                            waitset, return_when=asyncio.FIRST_COMPLETED
                        )

                        if stop_task is not None and stop_task in done:
                            next_task.cancel()
                            try:
                                await next_task
                            except (asyncio.CancelledError, BaseException):
                                pass
                            raise asyncio.CancelledError("stop_event set")

                        # next_task done
                        try:
                            chunk = next_task.result()
                        except StopAsyncIteration:
                            break

                        if not chunk:
                            continue
                        tmp_handle.write(chunk)
                        h.update(chunk)
                        bytes_total += len(chunk)
                        if opts.progress_cb is not None:
                            try:
                                opts.progress_cb(bytes_total, content_length)
                            except Exception:
                                logger.debug("progress_cb raised", exc_info=True)
                    tmp_handle.flush()
                    os.fsync(tmp_handle.fileno())
                finally:
                    if stop_task is not None and not stop_task.done():
                        stop_task.cancel()
    except BaseException:
        if tmp_handle is not None:
            tmp_handle.close()
            tmp_handle = None
        elif tmp_fd >= 0:
            os.close(tmp_fd)
            tmp_fd = -1
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if tmp_handle is not None:
            tmp_handle.close()
        elif tmp_fd >= 0:
            os.close(tmp_fd)

    return bytes_total, h.hexdigest()


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

async def download(
    url: str,
    dest_dir: str | os.PathLike,
    options: Optional[MediaOptions] = None,
) -> MediaResult:
    """Unified download entrypoint.

    Parameters
    ----------
    url : str
        The source URL or identifier (delegated backend may use a non-URL).
    dest_dir : path-like
        Final destination directory (typically under Z:/unifiedcollector/media/<source>/).
        Subprocess tiers download into a private tempdir and the caller
        ingests ``result.files`` into their own tree.
        httpx + delegated tiers write the final file directly under
        ``dest_dir`` using ``options.output_filename`` (or URL basename).
    options : MediaOptions, optional

    Returns
    -------
    MediaResult
    """
    opts = options or MediaOptions()
    dest_dir = Path(dest_dir)
    root = _option_vault_root(opts)
    assert_media_write_allowed(dest_dir / ".media_download_check", root=root, media_root=_option_media_root(opts))
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Pre-flight: rate-limiter hook (Agent C will populate)
    if opts.rate_limiter is not None:
        try:
            acq = getattr(opts.rate_limiter, "acquire", None)
            if acq is not None:
                res = acq()
                if asyncio.iscoroutine(res):
                    await res
        except Exception:
            logger.debug("rate_limiter.acquire raised", exc_info=True)

    backend = opts.backend if opts.backend != "auto" else pick_backend(url)

    started = time.perf_counter()

    try:
        if backend == "httpx":
            result = await _do_httpx(url, dest_dir, opts, started)
        elif backend == "gallery-dl":
            result = await _do_subprocess(url, dest_dir, opts, started, tool="gallery-dl")
        elif backend == "yt-dlp":
            result = await _do_subprocess(url, dest_dir, opts, started, tool="yt-dlp")
        elif backend == "delegated":
            result = await _do_delegated(url, dest_dir, opts, started)
        else:
            result = MediaResult(
                ok=False, url=url, backend=backend,
                error=f"unknown backend: {backend!r}",
                elapsed=time.perf_counter() - started,
            )
    except asyncio.CancelledError:
        return MediaResult(
            ok=False, url=url, backend=backend, cancelled=True,
            error="cancelled",
            elapsed=time.perf_counter() - started,
        )
    except Exception as exc:
        logger.exception("media_download.download failed for %s", url)
        return MediaResult(
            ok=False, url=url, backend=backend,
            error=f"{type(exc).__name__}: {exc}",
            elapsed=time.perf_counter() - started,
        )

    # GLOBAL I/O PACER (P2 review §5): after bytes hit Z, consume that many tokens
    # from the shared Redis bucket so aggregate write bandwidth across ALL collectors
    # stays under one ceiling. Fail-open + dormant unless MEDIA_IO_PACER_ENABLED=1, so
    # this can never block collection. Covers every tier (httpx/subprocess/delegated).
    if result.ok and result.bytes_total > 0:
        try:
            from .io_pacer import get_pacer
            await get_pacer().acquire(result.bytes_total)
        except Exception:
            logger.debug("io_pacer acquire skipped", exc_info=True)
    return result


# ---------------------------------------------------------------------------
# Per-backend implementations
# ---------------------------------------------------------------------------

def _filename_from_url(url: str, fallback: str = "download.bin") -> str:
    try:
        parsed = urlparse(url)
        base = os.path.basename(parsed.path) or fallback
        # Strip query fragments that snuck in
        return base.split("?")[0] or fallback
    except Exception:
        return fallback


async def _do_httpx(
    url: str, dest_dir: Path, opts: MediaOptions, started: float,
) -> MediaResult:
    name = opts.output_filename or _filename_from_url(url)
    final_path = dest_dir / name

    if final_path.exists() and not opts.overwrite:
        artifact = _commit_vault_artifact(
            url=url,
            backend="httpx",
            filename=name,
            source_path=final_path,
            dest_dir=dest_dir,
            opts=opts,
            expected_sha256=_sha256_of_file(final_path),
            delete_source=False,
        )
        return _artifact_result_to_media_result(
            artifact,
            url=url,
            backend="httpx",
            elapsed=time.perf_counter() - started,
        )

    # Wrap the inner download with retry. The retry decorator is dynamic
    # because max_retries is per-call.
    @async_retry(
        max_retries=max(1, opts.max_retries),
        base_delay=opts.retry_base_delay,
        retryable_exceptions=(Exception,),
    )
    async def _attempt():
        root = _option_vault_root(opts)
        tmp_fd, tmp_path = _open_download_temp(root, name, "httpx")
        try:
            bytes_total, sha = await _httpx_download_one(url, tmp_path, tmp_fd, opts)
            return tmp_path, bytes_total, sha
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    try:
        tmp_path, bytes_total, sha = await _attempt()
    except asyncio.CancelledError:
        # Surface cancel cleanly (don't let async_retry swallow as last_exc)
        raise
    except Exception as exc:
        return MediaResult(
            ok=False, url=url, backend="httpx",
            error=f"{type(exc).__name__}: {exc}",
            elapsed=time.perf_counter() - started,
        )

    artifact = _commit_vault_artifact(
        url=url,
        backend="httpx",
        filename=name,
        source_path=tmp_path,
        dest_dir=dest_dir,
        opts=opts,
        expected_sha256=sha,
        delete_source=True,
    )
    return _artifact_result_to_media_result(
        artifact,
        url=url,
        backend="httpx",
        elapsed=time.perf_counter() - started,
        fallback_bytes_total=bytes_total,
        fallback_sha256=sha,
    )


async def _do_subprocess(
    url: str, dest_dir: Path, opts: MediaOptions, started: float, *, tool: str,
) -> MediaResult:
    """gallery-dl / yt-dlp tier. Downloads into a private tempdir; caller
    is expected to move/ingest from ``result.files``.

    We do NOT auto-move files into ``dest_dir`` because each collector has
    distinct naming/sharding rules (handled in their own ingest step using
    ``file_naming.build_filename``). The tempdir is returned in
    ``MediaResult.subprocess_result.tempdir``.
    """
    # Tor proxy is propagated as ALL_PROXY env var for child processes.
    env = None
    if opts.tor_proxy_url:
        env = dict(os.environ)
        env["ALL_PROXY"] = opts.tor_proxy_url
        env["HTTPS_PROXY"] = opts.tor_proxy_url
        env["HTTP_PROXY"] = opts.tor_proxy_url

    if tool == "gallery-dl":
        sub_result = await _sub.gallery_dl_download(
            url,
            cookies_file=opts.cookies_file,
            extra_args=opts.extra_args,
            timeout=opts.timeout,
            progress_hook=opts.log_cb,
            stop_event=opts.stop_event,
        )
    else:
        sub_result = await _sub.yt_dlp_download(
            url,
            cookies_file=opts.cookies_file,
            extra_args=opts.extra_args,
            timeout=opts.timeout,
            progress_hook=opts.log_cb,
            stop_event=opts.stop_event,
        )
    # NOTE: _sub.* doesn't accept env yet (extending it is out of this
    # task's scope per READ-ONLY rule on src/core/* peers). Tor support
    # for subprocess tier will be wired by Agent C of Batch 2.
    if env is not None and "ALL_PROXY" in env:
        logger.debug(
            "media_download: tor_proxy_url set but subprocess tier env "
            "passthrough not yet implemented (Batch-2 Agent C will add)"
        )

    bytes_total = 0
    for p in sub_result.files:
        try:
            bytes_total += p.stat().st_size
        except OSError:
            pass

    return MediaResult(
        ok=sub_result.ok,
        url=url,
        backend=tool,
        files=list(sub_result.files),
        bytes_total=bytes_total,
        sha256=None,  # multi-file tier — caller hashes individually
        elapsed=time.perf_counter() - started,
        timed_out=sub_result.timed_out,
        cancelled=sub_result.cancelled,
        error=None if sub_result.ok else (sub_result.err_summary() or f"rc={sub_result.returncode}"),
        subprocess_result=sub_result,
    )


async def _do_delegated(
    url: str, dest_dir: Path, opts: MediaOptions, started: float,
) -> MediaResult:
    """Delegated backend — caller supplies a coroutine that writes the file.

    Used by Telethon (Telegram) / Baileys (WhatsApp) where the library has
    its own decryption/segment logic and cannot be replaced by gallery-dl.
    We still own the atomic-rename + sha256 + cancel envelope.
    """
    if opts.delegated_writer is None:
        return MediaResult(
            ok=False, url=url, backend="delegated",
            error="backend='delegated' but no delegated_writer supplied",
            elapsed=time.perf_counter() - started,
        )

    name = opts.output_filename or _filename_from_url(url, fallback="delegated.bin")
    final_path = dest_dir / name
    if final_path.exists() and not opts.overwrite:
        artifact = _commit_vault_artifact(
            url=url,
            backend="delegated",
            filename=name,
            source_path=final_path,
            dest_dir=dest_dir,
            opts=opts,
            expected_sha256=_sha256_of_file(final_path),
            delete_source=False,
        )
        return _artifact_result_to_media_result(
            artifact,
            url=url,
            backend="delegated",
            elapsed=time.perf_counter() - started,
        )

    tmp_fd, tmp_path = _open_download_temp(_option_vault_root(opts), name, "delegated")
    os.close(tmp_fd)

    # Race the writer against stop_event.
    writer_task = asyncio.create_task(opts.delegated_writer(tmp_path, opts))
    waitset: set[asyncio.Task] = {writer_task}
    stop_task: Optional[asyncio.Task] = None
    if opts.stop_event is not None:
        stop_task = asyncio.create_task(opts.stop_event.wait())
        waitset.add(stop_task)
    timeout_task = asyncio.create_task(asyncio.sleep(opts.timeout))
    waitset.add(timeout_task)

    cancelled = False
    timed_out = False
    err: Optional[str] = None

    try:
        done, _pending = await asyncio.wait(waitset, return_when=asyncio.FIRST_COMPLETED)
        if writer_task in done:
            try:
                writer_task.result()
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
        elif stop_task is not None and stop_task in done:
            cancelled = True
        else:
            timed_out = True
    finally:
        for t in (writer_task, timeout_task):
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, BaseException):
                    pass
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()
            try:
                await stop_task
            except (asyncio.CancelledError, BaseException):
                pass
        # Cleanup tmp on failure
        if (cancelled or timed_out or err) and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    if cancelled or timed_out or err:
        return MediaResult(
            ok=False, url=url, backend="delegated",
            cancelled=cancelled, timed_out=timed_out,
            error=err or ("cancelled" if cancelled else "timed_out"),
            elapsed=time.perf_counter() - started,
        )

    if not tmp_path.exists():
        return MediaResult(
            ok=False, url=url, backend="delegated",
            error="delegated_writer returned without writing the tmp file",
            elapsed=time.perf_counter() - started,
        )

    sha = _sha256_of_file(tmp_path)
    artifact = _commit_vault_artifact(
        url=url,
        backend="delegated",
        filename=name,
        source_path=tmp_path,
        dest_dir=dest_dir,
        opts=opts,
        expected_sha256=sha,
        delete_source=True,
    )
    return _artifact_result_to_media_result(
        artifact,
        url=url,
        backend="delegated",
        elapsed=time.perf_counter() - started,
        fallback_sha256=sha,
    )


# ---------------------------------------------------------------------------
# Convenience: hash a list of files (used by collectors after subprocess tier)
# ---------------------------------------------------------------------------

def hash_files(paths: Sequence[Path]) -> dict[Path, str]:
    """Compute SHA-256 for each path. Returns mapping path → hex digest.

    Convenience for collectors after a multi-file gallery-dl run.
    """
    return {p: _sha256_of_file(p) for p in paths if p.is_file()}


__all__ = [
    "MediaOptions",
    "MediaResult",
    "ProgressCB",
    "LogCB",
    "download",
    "pick_backend",
    "hash_files",
]
