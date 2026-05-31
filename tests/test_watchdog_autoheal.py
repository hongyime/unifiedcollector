"""Auto-heal watchdog test: proves a HUNG (non-crashing) collector is detected
and relaunched. This is the regression guard for the tiktok/youtube/whatsapp
silent-freeze class -- a task stuck inside collector.run() with no exception.

Run: python -m pytest tests/test_watchdog_autoheal.py -x  (or plain python).
"""
import asyncio
import time
import types


def _make_service():
    # Import lazily so this file can run even if other deps are heavy.
    from src.worker import WorkerService
    svc = WorkerService.__new__(WorkerService)
    # minimal manual init (avoid touching DB/pool)
    svc.pool = None
    svc._stop = asyncio.Event()
    svc._tasks = {}
    svc._crash_counts = {}
    svc._heartbeat = {}
    svc._started_at = time.monotonic()
    svc.watchdog_interval = 0.2          # fast watchdog for the test
    svc.max_restarts = 5
    svc.hang_timeout = 0.5               # tiny hang window for the test
    svc._dlq = None
    return svc


async def _run_test():
    svc = _make_service()

    relaunched = {"count": 0}

    # a "hung" coroutine: sets an initial heartbeat then sleeps forever (never
    # updates it again) -- exactly like a collector frozen inside run().
    async def hung_source():
        svc._heartbeat["x"] = time.monotonic()
        await asyncio.sleep(3600)

    def fake_launch(source, startup_delay=0.0):
        # record relaunch and install a fresh (still-hung) task
        relaunched["count"] += 1
        svc._tasks[source] = asyncio.create_task(hung_source())

    svc._launch = fake_launch

    # mark_source_dead stub
    async def fake_dead(source, reason, count):
        svc._dead_reason = reason
    svc._mark_source_dead = fake_dead

    # seed initial hung task
    svc._tasks["x"] = asyncio.create_task(hung_source())
    svc._crash_counts["x"] = 0
    await asyncio.sleep(0.05)  # let it set its heartbeat

    wd = asyncio.create_task(svc._watchdog_loop())

    # run long enough for the watchdog to detect the hang and escalate to the
    # max_restarts ceiling (each cycle ~ watchdog_interval + cancel-drain).
    await asyncio.sleep(6.0)
    svc._stop.set()
    wd.cancel()
    try:
        await wd
    except asyncio.CancelledError:
        pass
    # cancel any lingering hung tasks
    for t in svc._tasks.values():
        t.cancel()

    crashes = svc._crash_counts.get("x", 0)
    print(f"hang detections (crash_count): {crashes}")
    print(f"relaunches triggered: {relaunched['count']}")
    print(f"marked dead: {getattr(svc, '_dead_reason', None)}")

    # assertions: the hang MUST have been detected (crash_count rose) and the
    # source must have hit the ceiling -> marked dead (since every relaunch is
    # also hung). This proves both detection AND the give-up path.
    assert crashes >= 1, "watchdog never detected the hang!"
    assert getattr(svc, "_dead_reason", None) is not None, "never marked dead after max hangs"
    assert "hung" in svc._dead_reason
    print("PASS: watchdog detects hung collectors and escalates to dead")


if __name__ == "__main__":
    asyncio.run(_run_test())
