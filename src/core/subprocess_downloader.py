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
* Spawn the subprocess in a bounded worker thread using Popen with a hard
  timeout. Redirect stdout/stderr to temporary files, then keep only diagnostic
  tails so failed downloads are explainable without pipe deadlocks.
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
        """Backward-compatible diagnostic tail for logging and DB reasons."""
        return self.output_summary(limit)

    def output_summary(self, limit: int = 800) -> str:
        """Compact subprocess diagnostics, preferring stderr but using stdout too."""
        stderr = (self.stderr or "").strip()
        stdout = (self.stdout or "").strip()
        if stderr and stdout:
            half = max(1, limit // 2)
            return f"stderr: {stderr[-half:]}\nstdout: {stdout[-half:]}"
        if stderr:
            return stderr[-limit:]
        if stdout:
            return stdout[-limit:]
        if self.timed_out:
            return f"process timed out after {self.elapsed:.1f}s"
        if self.cancelled:
            return "process cancelled"
        if self.returncode not in (0, 101):
            return f"process exited with rc={self.returncode}; no stderr/stdout captured"
        return ""


# ---------------------------------------------------------------------------
# Internal: run subprocess with streaming + timeout + cancel
# ---------------------------------------------------------------------------

async def _drain_stream(stream: asyncio.StreamReader, sink: list[str], hook: Optional[Callable[[str], None]] = None):
    """Read lines from a subprocess stream into ``sink`` (and optional hook).

    Yields the event loop every 50 lines to prevent starvation of other tasks
    (timer callbacks, other coroutines) when subprocess produces large output.
    """
    line_count = 0
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
        line_count += 1
        if line_count % 50 == 0:
            await asyncio.sleep(0)   # yield to event loop every 50 lines


async def _run_subprocess(
    argv: Sequence[str],
    timeout: float,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    stop_event: Optional[asyncio.Event] = None,
    progress_hook: Optional[Callable[[str], None]] = None,
) -> tuple[int, str, str, bool, bool]:
    """Run a subprocess in a thread executor with hard wall-clock timeout.

    Uses subprocess.run() in a ThreadPoolExecutor thread instead of asyncio
    subprocess machinery, which can hang waiting for SIGCHLD in WSL2/Docker.
    Stdout/stderr are redirected to temporary files instead of pipes. This keeps
    the no-deadlock behavior while preserving a bounded diagnostic tail for
    failures.

    Returns (returncode, stdout, stderr, timed_out, cancelled).
    """
    import subprocess, concurrent.futures, os, signal

    timed_out = False
    cancelled = False
    stdout = ""
    stderr = ""
    try:
        output_tail_bytes = max(1024, int(os.getenv("SUBPROCESS_OUTPUT_TAIL_BYTES", "65536")))
    except ValueError:
        output_tail_bytes = 65536

    def _read_tail(f) -> str:
        try:
            f.flush()
            size = f.tell()
            start = max(0, size - output_tail_bytes)
            f.seek(start)
            data = f.read()
            text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
            if start > 0:
                return f"... truncated {start} bytes ...\n{text}"
            return text
        except Exception:
            return ""

    def _kill_pg(pid: int) -> None:
        """SIGKILL the whole process group, best-effort, NEVER blocking on wait().

        We deliberately do NOT call proc.wait() after killing. On WSL2/Docker the
        lost-SIGCHLD condition means wait() can hang forever even after the child is
        gone; reaping is left to the OS / init. A zombie is harmless and cheap."""
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass

    def _run_sync() -> tuple[int, str, str]:
        # Use Popen + communicate(timeout=) rather than subprocess.run(timeout=).
        # subprocess.run's TimeoutExpired path internally calls proc.kill() THEN
        # proc.wait() with no timeout — that second wait() is exactly what hangs on
        # a lost SIGCHLD, wedging this thread forever. Here we kill the process group
        # and return immediately WITHOUT a blocking re-wait.
        proc = None
        stdout_f = None
        stderr_f = None
        try:
            stdout_f = tempfile.TemporaryFile()
            stderr_f = tempfile.TemporaryFile()
            proc = subprocess.Popen(
                list(argv),
                stdout=stdout_f,
                stderr=stderr_f,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
            try:
                proc.communicate(timeout=timeout)
                rc = proc.returncode if proc.returncode is not None else -1
                return rc, _read_tail(stdout_f), _read_tail(stderr_f)
            except subprocess.TimeoutExpired:
                _kill_pg(proc.pid)
                # One short, bounded reap attempt — do NOT wait unbounded.
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                return -9, _read_tail(stdout_f), _read_tail(stderr_f)  # SIGKILL / timed out
        except Exception as exc:
            if proc is not None:
                _kill_pg(proc.pid)
            return -1, "", f"{type(exc).__name__}: {exc}"
        finally:
            for f in (stdout_f, stderr_f):
                try:
                    if f is not None:
                        f.close()
                except Exception:
                    pass

    loop = asyncio.get_running_loop()
    # daemon threads so an irrecoverably-wedged subprocess thread can never block
    # interpreter shutdown or pin the executor.
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="subproc",
    )
    # Hard wall-clock ceiling on the AWAIT itself. subprocess.run(timeout=...) relies
    # on SIGCHLD delivery to wake its internal wait(); in WSL2/Docker SIGCHLD can be
    # lost, so the child exits but the worker thread's wait() never returns and the
    # future never resolves — freezing the whole event loop (observed: youtube/yt-dlp
    # wedged the loop with the child already gone). This outer timeout guarantees the
    # loop is released; the orphaned thread is abandoned via shutdown(wait=False).
    outer_timeout = timeout + 60.0
    try:
        future = loop.run_in_executor(executor, _run_sync)
        # Poll the future with asyncio.sleep rather than awaiting it directly. A
        # bare `await future` / `asyncio.wait(future, timeout=...)` relies on the
        # executor calling loop.call_soon_threadsafe to resolve the future; if that
        # wakeup is lost (observed under WSL2/Docker with a wedged subprocess thread)
        # the await blocks the loop forever. asyncio.sleep ALWAYS wakes via the loop
        # timer, so this deadline check is guaranteed to fire.
        deadline = loop.time() + outer_timeout
        poll = 0.25
        while True:
            if future.done():
                break
            if stop_event is not None and stop_event.is_set():
                cancelled = True
                future.cancel()
                break
            if loop.time() >= deadline:
                logger.error("subprocess_downloader: hard timeout (%.0fs) — abandoning "
                             "wedged subprocess to unblock event loop", outer_timeout)
                timed_out = True
                future.cancel()
                break
            await asyncio.sleep(poll)
            if poll < 2.0:
                poll = min(poll * 1.5, 2.0)

        if not cancelled and not timed_out and future.done():
            rc, stdout, stderr = future.result()
        else:
            rc = -1
        if rc == -9:
            timed_out = True
    except concurrent.futures.CancelledError:
        cancelled = True
        rc = -1
    except Exception as e:
        logger.warning("subprocess_downloader: run_in_executor error: %s", e)
        rc = -1
    finally:
        executor.shutdown(wait=False)

    return rc, stdout, stderr, timed_out, cancelled


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

    argv: list[str] = [
        "gallery-dl",
        "--dest", tempdir,
        "--option", "downloader.http.timeout=30",   # per-request socket timeout
        "--option", "downloader.http.retries=1",    # fail fast, let caller retry
    ]
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

    argv: list[str] = ["yt-dlp", "--js-runtime", "node"]
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
        summary = DownloadResult(
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
            files=[],
            tempdir=Path(tempdir),
            elapsed=elapsed,
            timed_out=timed_out,
            cancelled=cancelled,
        ).output_summary(300)
        logger.info(
            "subprocess_downloader: %s rc=%s timed_out=%s cancelled=%s files=%d "
            "elapsed=%.1fs output_tail=%s",
            argv[0], rc, timed_out, cancelled, len(files), elapsed, summary,
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
