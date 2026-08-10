"""Auto-heal watchdog test: proves a HUNG (non-crashing) collector is detected
and relaunched. This is the regression guard for the tiktok/youtube/whatsapp
silent-freeze class -- a task stuck inside collector.run() with no exception.

Run: python -m pytest tests/test_watchdog_autoheal.py -x  (or plain python).
"""
import asyncio
import time
import types
from unittest.mock import AsyncMock


def _make_service():
    # Import lazily so this file can run even if other deps are heavy.
    from src.worker import WorkerService
    svc = WorkerService()
    # keep the fixture isolated from DB/pool work
    svc.pool = None
    svc._stop = asyncio.Event()
    svc._tasks = {}
    svc._crash_counts = {}
    svc._heartbeat = {}
    svc._started_at = time.monotonic()
    svc.watchdog_interval = 0.2          # fast watchdog for the test
    svc.max_restarts = 5
    svc.max_hang_cycles = 5
    svc.hang_timeout = 0.5               # tiny hang window for the test
    svc._dlq = None
    # clear mutable state that production __init__ creates
    svc._collectors = {}
    svc._hang_counts = {}
    svc._hang_progress_seen = {}
    svc._last_success_progress = {}
    svc._progress_baseline = {}
    svc._zero_progress_streak = {}
    svc.zero_progress_limit = 5
    svc.zero_progress_hard_limit = 12
    svc._auth_paused = {}
    svc._auth_pause_since = {}
    return svc


async def test_telegram_dlq_handler_retries_exact_media(monkeypatch):
    from src.worker import WorkerService

    svc = WorkerService()
    svc.pool = types.SimpleNamespace()
    worker_a = object()
    worker_b = object()
    retry = AsyncMock(side_effect=[None, True])
    svc._collectors = {
        "telegram": types.SimpleNamespace(
            _workers=[worker_a, worker_b],
            download_message_media=retry,
        ),
    }

    handler = svc._make_dlq_handler("telegram")
    await handler({
        "entity_id": "-100123",
        "content_id": "-100123_456",
    })

    assert retry.await_count == 2
    retry.assert_any_await(456, worker=worker_a, chat_id="-100123")
    retry.assert_any_await(456, worker=worker_b, chat_id="-100123")


async def test_dlq_handler_requeues_non_exact_rows(monkeypatch):
    from src.worker import WorkerService

    svc = WorkerService()
    requeue = AsyncMock()
    svc._requeue_dlq_target = requeue

    handler = svc._make_dlq_handler("telegram")
    await handler({
        "entity_id": "-100123",
        "content_id": "not-message-media",
    })

    requeue.assert_awaited_once_with("telegram", "-100123")


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
    async def fake_exit(reason):
        svc._self_heal_reason = reason
    svc._self_heal_exit = fake_exit

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

    hangs = svc._hang_counts.get("x", 0)
    print(f"hang detections: {hangs}")
    print(f"relaunches triggered: {relaunched['count']}")
    print(f"marked dead: {getattr(svc, '_dead_reason', None)}")

    # assertions: the hang MUST have been detected (crash_count rose) and the
    # source must have hit the ceiling -> marked dead (since every relaunch is
    # also hung). This proves both detection AND the give-up path.
    assert hangs >= 1, "watchdog never detected the hang!"
    assert getattr(svc, "_dead_reason", None) is not None, "never marked dead after max hangs"
    assert "hung" in svc._dead_reason
    assert "hung" in getattr(svc, "_self_heal_reason", "")
    print("PASS: watchdog detects hung collectors and escalates to dead")


async def _run_zero_progress_soft_test():
    """Alive task (fresh heartbeat, never hangs) with a zero-progress streak at
    the soft limit must be cancelled for a fresh-collector relaunch."""
    svc = _make_service()
    relaunched = {"count": 0}

    async def alive_source():
        # Keeps beating its heartbeat forever -> never trips hang detection.
        while True:
            svc._heartbeat["z"] = time.monotonic()
            await asyncio.sleep(0.05)

    def fake_launch(source, startup_delay=0.0):
        relaunched["count"] += 1
        svc._zero_progress_streak[source] = 0
        svc._tasks[source] = asyncio.create_task(alive_source())

    svc._launch = fake_launch

    async def fake_dead(source, reason, count):
        svc._dead_reason = reason
    svc._mark_source_dead = fake_dead
    async def fake_exit(reason):
        svc._self_heal_reason = reason
    svc._self_heal_exit = fake_exit

    svc._tasks["z"] = asyncio.create_task(alive_source())
    svc._crash_counts["z"] = 0
    # seed a streak AT the soft limit; heartbeat is fresh (not hung)
    svc._zero_progress_streak["z"] = svc.zero_progress_limit
    await asyncio.sleep(0.05)

    wd = asyncio.create_task(svc._watchdog_loop())
    await asyncio.sleep(2.0)
    svc._stop.set()
    wd.cancel()
    try:
        await wd
    except asyncio.CancelledError:
        pass
    for t in svc._tasks.values():
        t.cancel()

    print(f"soft relaunches: {relaunched['count']}, dead: {getattr(svc,'_dead_reason',None)}")
    # The soft tier cancels the task; the done-branch relaunches it next pass.
    assert relaunched["count"] >= 1, "zero-progress soft tier never relaunched!"
    print("PASS: zero-progress soft tier cancels + relaunches an alive wedged source")


async def _run_zero_progress_hard_test():
    """Streak at the hard limit must mark the source dead (soft relaunch failed)."""
    svc = _make_service()

    async def alive_source():
        while True:
            svc._heartbeat["h"] = time.monotonic()
            await asyncio.sleep(0.05)

    def fake_launch(source, startup_delay=0.0):
        svc._tasks[source] = asyncio.create_task(alive_source())
    svc._launch = fake_launch

    async def fake_dead(source, reason, count):
        svc._dead_reason = reason
    svc._mark_source_dead = fake_dead
    async def fake_exit(reason):
        svc._self_heal_reason = reason
    svc._self_heal_exit = fake_exit

    svc._tasks["h"] = asyncio.create_task(alive_source())
    svc._crash_counts["h"] = 0
    svc._zero_progress_streak["h"] = svc.zero_progress_hard_limit
    await asyncio.sleep(0.05)

    wd = asyncio.create_task(svc._watchdog_loop())
    await asyncio.sleep(1.0)
    svc._stop.set()
    wd.cancel()
    try:
        await wd
    except asyncio.CancelledError:
        pass
    for t in svc._tasks.values():
        t.cancel()

    print(f"hard dead reason: {getattr(svc,'_dead_reason',None)}")
    assert getattr(svc, "_dead_reason", None) is not None, "hard tier never marked dead!"
    assert "zero-progress" in svc._dead_reason
    assert "zero-progress" in getattr(svc, "_self_heal_reason", "")
    print("PASS: zero-progress hard tier marks the source dead")


if __name__ == "__main__":
    asyncio.run(_run_test())
    asyncio.run(_run_zero_progress_soft_test())
    asyncio.run(_run_zero_progress_hard_test())
