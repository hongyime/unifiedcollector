"""Subprocess downloader — canonical wrapper around gallery-dl / yt-dlp.

Consolidates the duplicated launch-subprocess + tempdir + cookies + ingest
pattern that lives across collectors (currently most mature in
src/collectors/tiktok.py:_collect_via_gallery_dl / _collect_via_yt_dlp).

What it does
------------
* Build the right argv for gallery-dl or yt-dlp.
* Inject cookies file when present.
* Add the safety ``--`` separator before any URL (yt-dlp/gallery-dl arg
  injection guard; same pattern the audit added to tiktok.py:359 and
  youtube.py:396).
* Spawn the subprocess via asyncio.create_subprocess_exec (true non-
  blocking; not the run_in_executor pattern that pinned a thread per
  download). Capture stdout/stderr with a hard timeout. Stream the
  output through a callback hook so callers can tail-log progress.
* Return ``DownloadResult`` with: returncode, stdout, stderr, files
  (list of Path objects under tempdir), tempdir Path, elapsed seconds.

What it does NOT do
-------------------
* Doesn't ingest files into the unified DB / drive — that's collector-
  specific (each source has its own schema). Caller iterates
  ``result.files`` and does the upsert/move.
* Doesn't manage cookies expiry / refresh.
* Doesn't choose between gallery-dl and yt-dlp — caller picks based on
  source/URL pattern (gallery-dl handles tiktok/instagram/twitter
  natively; yt-dlp is the universal fallback).

Why subprocess_exec, not subprocess.run in executor
---------------------------------------------------
``await asyncio.create_subprocess_exec`` integrates with the asyncio
event loop directly so we can stream stderr/stdout in chunks (useful
for catching gallery-dl progress lines mid-flight) and cancel cleanly
on ``stop_event.set()``. The toolkit pattern of
``loop.run_in_executor(None, subprocess.run, ...)`` works but pins one
thread per concurrent download.

Tool detection
--------------
``check_tool(name)`` -> bool, cached. Use this in collector __init__ to
gate which fallback paths to expose.

Cookies file format
-------------------
Both gallery-dl and yt-dlp accept Netscape-format cookies.txt. We don't
parse it here; we just pass the path through. Caller is responsible
for ensuring the file is current (e.g. exported from the active session
manager via existing instagram session-manager helpers).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool detection (cached)
# ---------------------------------------------------------------------------

_TOOL_CACHE: dict[str, bool] = {}


def check_tool(name: str) -> bool:
    """Return True if ``name`` is on PATH. Cached.

    Re-detects only if explicitly cleared via ``check_tool.clear_cache()``.
    """
    if name in _TOOL_CACHE:
        return _TOOL_CACHE[name]
    found = shutil.which(name) is not None
    _TOOL_CACHE[name] = found
    if found:
        logger.debug("subprocess_downloader: %s available at %s", name, shutil.which(name))
    else:
        logger.info("subprocess_downloader: %s NOT on PATH", name)
    return found


def _clear_tool_cache():
    _TOOL_CACHE.clear()


check_tool.clear_cache = _clear_tool_cache  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class DownloadResult:
    """Outcome of one subprocess download invocation."""
    returncode: int
    stdout: str
    stderr: str
    files: list[Path]
    tempdir: Path
    elapsed: float
    timed_out: bool = False
    cancelled: bool = False

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def ok(self) -> bool:
        # yt-dlp returns 101 when ``--max-downloads`` cap was hit; that's
        # still a successful run from our perspective.
        return (self.returncode in (0, 101)) and not self.timed_out and not self.cancelled

    def err_summary(self, limit: int = 800) -> str:
        """Compact tail of stderr for logging."""
        return (self.stderr or "")[-limit:]


# ---------------------------------------------------------------------------
# Internal: run subprocess with streaming + timeout + cancel
# ---------------------------------------------------------------------------

async def _drain_stream(stream: asyncio.StreamReader, sink: list[str], hook: Optional[Callable[[str], None]] = None):
    """Read lines from a subprocess stream into ``sink`` (and optional hook)."""
    while True:
        line = await stream.readline()
        if not line:
            return
        try:
            text = line.decode("utf-8", errors="replace")
        except Exception:
            text = line.decode("latin-1", errors="replace")
        sink.append(text)
        if hook is not None:
            try:
                hook(text.rstrip("\n"))
            except Exception:
                # never let a bad hook kill the download
                logger.debug("subprocess_downloader: progress hook raised", exc_info=True)


async def _run_subprocess(
    argv: Sequence[str],
    timeout: float,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    stop_event: Optional[asyncio.Event] = None,
    progress_hook: Optional[Callable[[str], None]] = None,
) -> tuple[int, str, str, bool, bool]:
    """Run a subprocess with streamed capture, hard timeout, optional cancel.

    Returns (returncode, stdout, stderr, timed_out, cancelled).
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )

    out_chunks: list[str] = []
    err_chunks: list[str] = []
    timed_out = False
    cancelled = False

    out_task = asyncio.create_task(_drain_stream(proc.stdout, out_chunks, progress_hook))
    err_task = asyncio.create_task(_drain_stream(proc.stderr, err_chunks, progress_hook))

    # Race three things: process exit, hard timeout, and stop_event.
    proc_task = asyncio.create_task(proc.wait())
    timeout_task = asyncio.create_task(asyncio.sleep(timeout))
    waitset = {proc_task, timeout_task}
    stop_task: Optional[asyncio.Task] = None
    if stop_event is not None:
        stop_task = asyncio.create_task(stop_event.wait())
        waitset.add(stop_task)

    try:
        done, pending = await asyncio.wait(waitset, return_when=asyncio.FIRST_COMPLETED)

        if proc_task in done:
            # Process finished naturally
            pass
        elif stop_task is not None and stop_task in done:
            cancelled = True
        else:
            timed_out = True

        # If proc still running, kill it.
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                logger.error("subprocess_downloader: kill did not terminate within 10s")

        # Cancel remaining race tasks (timeout_task / stop_task)
        for t in pending:
            if t is not proc_task:
                t.cancel()

        # Drain residual streams (they should close once proc exits)
        for t in (out_task, err_task):
            try:
                await asyncio.wait_for(t, timeout=2)
            except asyncio.TimeoutError:
                t.cancel()
            except Exception:
                pass

        rc = proc.returncode if proc.returncode is not None else -1
    finally:
        for t in (out_task, err_task, timeout_task):
            if not t.done():
                t.cancel()
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()

    return rc, "".join(out_chunks), "".join(err_chunks), timed_out, cancelled


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def gallery_dl_download(
    url: str,
    *,
    cookies_file: Optional[str] = None,
    extra_args: Optional[Sequence[str]] = None,
    timeout: float = 300.0,
    progress_hook: Optional[Callable[[str], None]] = None,
    stop_event: Optional[asyncio.Event] = None,
    write_metadata: bool = True,
    no_mtime: bool = True,
    verbose: bool = True,
    tempdir: Optional[str] = None,
) -> DownloadResult:
    """Run ``gallery-dl`` against ``url``, return ``DownloadResult``.

    Files are downloaded into a private tempdir which is RETURNED on the
    result (caller is responsible for cleanup once ingestion is done).
    Use the ``managed_tempdir()`` async context manager if you don't need
    to inspect the tempdir post-cleanup.
    """
    if not check_tool("gallery-dl"):
        raise RuntimeError("gallery-dl not found on PATH")

    own_tempdir = tempdir is None
    if own_tempdir:
        tempdir = tempfile.mkdtemp(prefix="gdl_")

    argv: list[str] = ["gallery-dl", "--dest", tempdir]
    if no_mtime:
        argv.append("--no-mtime")
    if write_metadata:
        argv.append("--write-metadata")
    if verbose:
        argv.append("-v")
    if cookies_file:
        argv.extend(["--cookies", cookies_file])
    if extra_args:
        argv.extend(list(extra_args))
    # `--` ensures URL beginning with `--` is treated as positional, not an arg.
    argv.append("--")
    argv.append(url)

    return await _run_and_collect(argv, tempdir, timeout, progress_hook, stop_event)


async def yt_dlp_download(
    url: str,
    *,
    cookies_file: Optional[str] = None,
    output_template: Optional[str] = None,
    max_downloads: Optional[int] = 50,
    retries: int = 3,
    impersonate: Optional[str] = "chrome",
    write_thumbnail: bool = True,
    no_overwrites: bool = True,
    extra_args: Optional[Sequence[str]] = None,
    timeout: float = 300.0,
    progress_hook: Optional[Callable[[str], None]] = None,
    stop_event: Optional[asyncio.Event] = None,
    tempdir: Optional[str] = None,
) -> DownloadResult:
    """Run ``yt-dlp`` against ``url``, return ``DownloadResult``."""
    if not check_tool("yt-dlp"):
        raise RuntimeError("yt-dlp not found on PATH")

    own_tempdir = tempdir is None
    if own_tempdir:
        tempdir = tempfile.mkdtemp(prefix="ytdlp_")

    if output_template is None:
        output_template = os.path.join(tempdir, "%(id)s.%(ext)s")

    argv: list[str] = ["yt-dlp"]
    if impersonate:
        argv.extend(["--impersonate", impersonate])
    if write_thumbnail:
        argv.append("--write-thumbnail")
    if no_overwrites:
        argv.append("--no-overwrites")
    argv.extend(["-o", output_template])
    if max_downloads is not None:
        argv.extend(["--max-downloads", str(max_downloads)])
    argv.extend(["--retries", str(retries)])
    argv.extend(["--socket-timeout", "30"])
    if cookies_file:
        argv.extend(["--cookies", cookies_file])
    if extra_args:
        argv.extend(list(extra_args))
    argv.append("--")
    argv.append(url)

    return await _run_and_collect(argv, tempdir, timeout, progress_hook, stop_event)


async def _run_and_collect(
    argv: Sequence[str],
    tempdir: str,
    timeout: float,
    progress_hook: Optional[Callable[[str], None]],
    stop_event: Optional[asyncio.Event],
) -> DownloadResult:
    started = time.perf_counter()
    rc, stdout, stderr, timed_out, cancelled = await _run_subprocess(
        argv, timeout=timeout, progress_hook=progress_hook, stop_event=stop_event,
    )
    elapsed = time.perf_counter() - started

    files: list[Path] = []
    try:
        for p in Path(tempdir).rglob("*"):
            if p.is_file():
                files.append(p)
    except Exception:
        logger.exception("subprocess_downloader: tempdir walk failed for %s", tempdir)

    if rc != 0 and rc != 101:
        logger.info(
            "subprocess_downloader: %s rc=%s timed_out=%s cancelled=%s files=%d "
            "elapsed=%.1fs stderr_tail=%s",
            argv[0], rc, timed_out, cancelled, len(files), elapsed, (stderr or "")[-300:],
        )
    else:
        logger.info(
            "subprocess_downloader: %s ok files=%d elapsed=%.1fs",
            argv[0], len(files), elapsed,
        )

    return DownloadResult(
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
        files=files,
        tempdir=Path(tempdir),
        elapsed=elapsed,
        timed_out=timed_out,
        cancelled=cancelled,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@asynccontextmanager
async def managed_tempdir(prefix: str = "dl_"):
    """async-context tempdir that cleans up when the block exits.

    Usage::

        async with managed_tempdir("tiktok_") as td:
            result = await gallery_dl_download(url, tempdir=td)
            # use result.files
        # td is gone here
    """
    td = tempfile.mkdtemp(prefix=prefix)
    try:
        yield td
    finally:
        try:
            shutil.rmtree(td, ignore_errors=True)
        except Exception:
            logger.debug("managed_tempdir cleanup failed for %s", td, exc_info=True)


__all__ = [
    "DownloadResult",
    "check_tool",
    "gallery_dl_download",
    "yt_dlp_download",
    "managed_tempdir",
]
