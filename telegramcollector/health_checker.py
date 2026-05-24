"""
health_checker.py — Periodic health monitor for the worker.py local runtime.

Runs background health checks at a configurable interval and fires registered
recovery callbacks when a component is detected as unhealthy.
"""
import asyncio
import logging
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


class HealthChecker:
    """
    Lightweight health monitor.

    Usage (from worker.py):
        hc = HealthChecker(client=None, face_processor=fp,
                           processing_queue=pq, check_interval=1800)
        hc.register_recovery('telegram', worker._recover_telegram)
        hc.register_recovery('hub_access', worker._recover_hub_access)
        await hc.start()
        # later:
        hc.client = connected_client
    """

    def __init__(self, client, face_processor, processing_queue, check_interval: int = 1800):
        self.client = client
        self.face_processor = face_processor
        self.processing_queue = processing_queue
        self.check_interval = check_interval

        self._recovery: Dict[str, Callable] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def register_recovery(self, name: str, callback: Callable) -> None:
        self._recovery[name] = callback

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="health_checker")
        logger.info("HealthChecker started (interval=%ds)", self.check_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("HealthChecker stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.sleep(self.check_interval)),
                    timeout=self.check_interval + 5,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if not self._running:
                    break

            await self._run_checks()

    async def _run_checks(self) -> None:
        # Telegram connectivity
        if self.client is not None:
            try:
                if not self.client.is_connected():
                    logger.warning("HealthChecker: Telegram client disconnected — firing recovery")
                    await self._recover('telegram')
            except Exception as exc:
                logger.warning("HealthChecker: Telegram check error: %s", exc)

        # Processing queue backpressure
        if self.processing_queue is not None:
            try:
                from shared.processing_queue import BackpressureState
                state = self.processing_queue.get_backpressure_state()
                if state == BackpressureState.CRITICAL:
                    logger.warning("HealthChecker: queue in CRITICAL backpressure state")
            except Exception:
                pass

    async def _recover(self, name: str) -> None:
        cb = self._recovery.get(name)
        if cb is None:
            return
        try:
            if asyncio.iscoroutinefunction(cb):
                await cb()
            else:
                cb()
        except Exception as exc:
            logger.error("HealthChecker: recovery '%s' raised: %s", name, exc)
