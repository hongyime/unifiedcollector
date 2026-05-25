"""Behavioral verification for src/core/subprocess_downloader.

Doesn't require gallery-dl/yt-dlp to actually be installed; instead, we
exercise the internal _run_subprocess function with simple shell
commands that exist on Windows + Linux (python itself).
"""
import asyncio
import sys
import os
from pathlib import Path
import tempfile
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.subprocess_downloader import (  # noqa: E402
    _run_subprocess,
    check_tool,
    DownloadResult,
    managed_tempdir,
)


def assert_eq(name, got, want):
    if got == want:
        print(f"  OK    {name}: {got!r}")
    else:
        print(f"  FAIL  {name}: got={got!r} want={want!r}")
        raise SystemExit(1)


def assert_true(name, cond, detail=""):
    print(f"  {'OK' if cond else 'FAIL'}    {name}{(' ' + detail) if detail else ''}")
    if not cond:
        raise SystemExit(1)


PYBIN = sys.executable


async def main():
    print("=" * 60)
    print("Test 1: check_tool() detects existing + missing tools")
    print("=" * 60)
    assert_eq("python detected", check_tool(sys.argv[0].split(os.sep)[-1].rsplit(".", 1)[0]) or check_tool("python"), True)
    assert_eq("nonexistent tool", check_tool("definitely_not_a_real_tool_xyz123"), False)
    # Cached
    assert_eq("cached re-call", check_tool("definitely_not_a_real_tool_xyz123"), False)

    print("\n" + "=" * 60)
    print("Test 2: _run_subprocess captures stdout + returncode")
    print("=" * 60)
    rc, stdout, stderr, timed_out, cancelled = await _run_subprocess(
        [PYBIN, "-c", "import sys; print('hello'); sys.stderr.write('err\\n')"],
        timeout=10,
    )
    assert_eq("rc 0", rc, 0)
    assert_true("stdout has 'hello'", "hello" in stdout, f"stdout={stdout!r}")
    assert_true("stderr has 'err'", "err" in stderr, f"stderr={stderr!r}")
    assert_eq("not timed out", timed_out, False)
    assert_eq("not cancelled", cancelled, False)

    print("\n" + "=" * 60)
    print("Test 3: nonzero returncode propagates")
    print("=" * 60)
    rc, _, _, _, _ = await _run_subprocess(
        [PYBIN, "-c", "import sys; sys.exit(7)"],
        timeout=10,
    )
    assert_eq("rc 7", rc, 7)

    print("\n" + "=" * 60)
    print("Test 4: timeout kills the process")
    print("=" * 60)
    import time
    t0 = time.perf_counter()
    rc, _, _, timed_out, cancelled = await _run_subprocess(
        [PYBIN, "-c", "import time; time.sleep(30)"],
        timeout=1.0,
    )
    el = time.perf_counter() - t0
    assert_true("returned within ~3s (timeout 1s + cleanup buffer)", el < 5, f"elapsed={el:.2f}s")
    assert_eq("timed_out flag set", timed_out, True)
    assert_true("rc != 0", rc != 0, f"rc={rc}")

    print("\n" + "=" * 60)
    print("Test 5: stop_event cancels mid-run")
    print("=" * 60)
    stop = asyncio.Event()
    async def cancel_after(s):
        await asyncio.sleep(s)
        stop.set()
    t0 = time.perf_counter()
    cancel_task = asyncio.create_task(cancel_after(0.5))
    rc, _, _, timed_out, cancelled = await _run_subprocess(
        [PYBIN, "-c", "import time; time.sleep(30)"],
        timeout=10.0,
        stop_event=stop,
    )
    await cancel_task
    el = time.perf_counter() - t0
    assert_true("returned within ~3s (cancel @0.5s)", el < 4, f"elapsed={el:.2f}s")
    assert_eq("cancelled flag set", cancelled, True)
    assert_eq("not timed_out", timed_out, False)

    print("\n" + "=" * 60)
    print("Test 6: progress_hook receives lines as they're emitted")
    print("=" * 60)
    received = []
    rc, _, _, _, _ = await _run_subprocess(
        [PYBIN, "-c",
         "import sys, time\n"
         "for i in range(3):\n"
         "    print(f'line{i}'); sys.stdout.flush()\n"
         "    time.sleep(0.05)\n"],
        timeout=10,
        progress_hook=received.append,
    )
    assert_eq("3 lines received", len([x for x in received if x.startswith("line")]), 3)

    print("\n" + "=" * 60)
    print("Test 7: managed_tempdir cleans up after block")
    print("=" * 60)
    captured = None
    async with managed_tempdir("test_") as td:
        captured = td
        Path(td, "x.txt").write_text("hi")
        assert_true("td exists during block", os.path.isdir(td))
        assert_true("file in td", os.path.isfile(os.path.join(td, "x.txt")))
    assert_true("td gone after block", not os.path.exists(captured), f"td={captured}")

    print("\n" + "=" * 60)
    print("Test 8: progress_hook exception doesn't kill process")
    print("=" * 60)
    def bad_hook(line):
        raise RuntimeError("hook is broken")
    rc, stdout, _, _, _ = await _run_subprocess(
        [PYBIN, "-c", "print('survived')"],
        timeout=10,
        progress_hook=bad_hook,
    )
    assert_eq("rc still 0", rc, 0)
    assert_true("output captured", "survived" in stdout, f"stdout={stdout!r}")

    print("\n" + "=" * 60)
    print("Test 9: DownloadResult.ok logic")
    print("=" * 60)
    r = DownloadResult(returncode=0, stdout="", stderr="", files=[], tempdir=Path("."), elapsed=1.0)
    assert_eq("rc 0 -> ok", r.ok, True)
    r = DownloadResult(returncode=101, stdout="", stderr="", files=[], tempdir=Path("."), elapsed=1.0)
    assert_eq("rc 101 (yt-dlp max-downloads) -> ok", r.ok, True)
    r = DownloadResult(returncode=1, stdout="", stderr="", files=[], tempdir=Path("."), elapsed=1.0)
    assert_eq("rc 1 -> not ok", r.ok, False)
    r = DownloadResult(returncode=0, stdout="", stderr="", files=[], tempdir=Path("."), elapsed=1.0, timed_out=True)
    assert_eq("timed_out -> not ok even with rc 0", r.ok, False)

    print("\n" + "=" * 60)
    print("ALL SUBPROCESS-DOWNLOADER TESTS PASSED")
    print("=" * 60)


asyncio.run(main())
