import asyncio
import json
import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from src.collectors import get_collector, list_sources
from src.core.drive_check import check_drive, wait_for_drive
from src.db.connection import get_pool, close_pool

logger = logging.getLogger(__name__)


class WorkerService:
    """Runs collectors as supervised background tasks with watchdog restart."""

    def __init__(self):
        self.pool = None
        self._stop = asyncio.Event()
        self._tasks: dict[str, asyncio.Task] = {}
        self._crash_counts: dict[str, int] = {}
        self._started_at: float = 0
        self.watchdog_interval = 30
        self.max_restarts = 5

    async def start(self, sources: list[str]):
        logger.info("Worker service starting with sources: %s", sources)
        self._started_at = time.monotonic()

        if not check_drive():
            logger.warning("Drive not available, waiting...")
            ok = await asyncio.get_event_loop().run_in_executor(
                None, wait_for_drive,
            )
            if not ok:
                logger.error("Drive never appeared, exiting")
                return

        self.pool = await get_pool()
        await self._init_db()

        # Warmup WSL2 network stack before launching collectors.
        # Without this, the first httpx request from any collector hits a kernel
        # D-state (uninterruptible sleep) that freezes the entire asyncio event loop.
        await self._warmup_network()

        self._install_signal_handlers()

        for i, source in enumerate(sources):
            self._launch(source, startup_delay=i * 3.0)

        watchdog = asyncio.create_task(self._watchdog_loop())
        reporter = asyncio.create_task(self._health_reporter())

        await self._stop.wait()

        watchdog.cancel()
        reporter.cancel()
        for t in self._tasks.values():
            t.cancel()
        all_tasks = [watchdog, reporter, *self._tasks.values()]
        await asyncio.gather(*all_tasks, return_exceptions=True)

        await self._report_health("stopped")
        await close_pool()
        logger.info("Worker service stopped")

    async def _init_db(self):
        schema_dir = Path(__file__).resolve().parent.parent / "db" / "schemas"
        async with self.pool.acquire() as conn:
            for sql_file in sorted(schema_dir.glob("*.sql")):
                await conn.execute(sql_file.read_text())

    async def _warmup_network(self):
        """Prime WSL2 network via a thread — kernel-level hang cannot freeze the event loop."""
        import urllib.request, concurrent.futures
        def _probe():
            try:
                r = urllib.request.urlopen(
                    "http://connectivitycheck.gstatic.com/generate_204", timeout=20
                )
                return f"ok:{r.status}"
            except Exception as exc:
                return f"warn:{exc}"
        logger.info("worker: warming up network stack...")
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            try:
                result = await asyncio.wait_for(loop.run_in_executor(pool, _probe), timeout=22.0)
                logger.info("worker: network warmup %s", result)
            except asyncio.TimeoutError:
                logger.warning("worker: network warmup timed out (non-fatal)")
            except Exception as exc:
                logger.warning("worker: network warmup failed (non-fatal): %s", exc)

    def _install_signal_handlers(self):
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_signal)
            except NotImplementedError:
                signal.signal(sig, lambda *_: self._handle_signal())

    def _handle_signal(self):
        logger.info("Shutdown signal received")
        self._stop.set()

    def _launch(self, source: str, startup_delay: float = 0.0):
        if source in self._tasks and not self._tasks[source].done():
            return
        self._crash_counts.setdefault(source, 0)
        task = asyncio.create_task(self._run_source(source, startup_delay=startup_delay), name=f"worker-{source}")
        self._tasks[source] = task
        logger.info("Launched worker for %s", source)

    async def _run_source(self, source: str, startup_delay: float = 0.0):
        if startup_delay > 0:
            logger.info("worker/%s: staggered startup, waiting %.0fs", source, startup_delay)
            await asyncio.sleep(startup_delay)
        collector = get_collector(source)
        collector.set_pool(self.pool)

        while not self._stop.is_set():
            try:
                targets = await self._load_targets(source)
                if not targets:
                    logger.debug("No targets for %s, sleeping 60s", source)
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=60)
                    except asyncio.TimeoutError:
                        pass
                    continue

                logger.info("Running %s with %d targets", source, len(targets))
                await collector.run(targets)
                self._crash_counts[source] = 0

                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=300)
                except asyncio.TimeoutError:
                    pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._crash_counts[source] += 1
                count = self._crash_counts[source]
                logger.error("%s crashed (%d/%d): %r", source, count, self.max_restarts, e,
                             exc_info=True)
                if count >= self.max_restarts:
                    logger.error("%s exceeded max restarts, giving up", source)
                    break
                backoff = min(300, 30 * (2 ** (count - 1)))
                logger.info("Restarting %s in %ds", source, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass

    async def _load_targets(self, source: str) -> list[str]:
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT target_id FROM collection_targets "
                    "WHERE source = $1 AND status IN ('pending', 'error') "
                    "ORDER BY priority DESC",
                    source,
                )
            return [r["target_id"] for r in rows]
        except Exception:
            # Don't pretend "no targets" — that hides real DB outages.
            # Log with stack and return empty so the run loop sleeps and
            # retries, but operators can see the failure.
            logger.exception("Failed to load targets for source=%s", source)
            return []

    async def _watchdog_loop(self):
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.watchdog_interval)
                break
            except asyncio.TimeoutError:
                pass

            for source, task in list(self._tasks.items()):
                if task.done():
                    exc = task.exception() if not task.cancelled() else None
                    if exc:
                        logger.warning("Watchdog: %s died (%s), relaunching", source, exc)
                    else:
                        logger.info("Watchdog: %s finished, relaunching", source)
                    crashes = self._crash_counts.get(source, 0)
                    if crashes < self.max_restarts:
                        self._launch(source)

    async def _health_reporter(self):
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
                break
            except asyncio.TimeoutError:
                pass
            await self._report_health("running")

    async def _report_health(self, status: str):
        try:
            active = sum(1 for t in self._tasks.values() if not t.done())
            total_crashes = sum(self._crash_counts.values())
            uptime = int(time.monotonic() - self._started_at)
            payload = json.dumps({
                "status": status,
                "active_workers": active,
                "total_workers": len(self._tasks),
                "crash_count": total_crashes,
                "uptime_seconds": uptime,
                "drive_ok": check_drive(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO service_cursors (service, last_processed_at, status) "
                    "VALUES ('_worker', NOW(), $1) "
                    "ON CONFLICT (service) DO UPDATE "
                    "SET last_processed_at = NOW(), status = $1",
                    status,
                )
        except Exception:
            # Operators won't see worker liveness if this fails silently.
            # Use warning + stack so degraded health is observable.
            logger.warning("Health report failed", exc_info=True)

    def get_health(self) -> dict:
        active = sum(1 for t in self._tasks.values() if not t.done())
        return {
            "running": not self._stop.is_set(),
            "active_workers": active,
            "total_workers": len(self._tasks),
            "crash_count": sum(self._crash_counts.values()),
            "uptime_seconds": int(time.monotonic() - self._started_at) if self._started_at else 0,
            "sources": {
                s: {
                    "alive": not t.done(),
                    "crashes": self._crash_counts.get(s, 0),
                }
                for s, t in self._tasks.items()
            },
        }


_instance: WorkerService | None = None


async def run_worker(sources: list[str]):
    global _instance
    _instance = WorkerService()
    await _instance.start(sources)


def get_worker_health() -> dict:
    if _instance is None:
        return {"running": False, "active_workers": 0, "total_workers": 0, "crash_count": 0}
    return _instance.get_health()
