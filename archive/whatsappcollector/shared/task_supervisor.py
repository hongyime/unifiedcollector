import asyncio
import time
from collections import deque
from collections.abc import Callable

import structlog

logger = structlog.get_logger(__name__)


class TaskSupervisor:
    """Asyncio task watchdog with automatic restart and flap detection."""

    def __init__(self, name: str, coro_fn: Callable, restart_delay: float = 5.0):
        self.name = name
        self.coro_fn = coro_fn
        self.restart_delay = restart_delay
        self._task: asyncio.Task | None = None
        self._restart_count: int = 0
        self._restart_timestamps: deque[float] = deque()
        self.running: bool = False

    async def start(self) -> None:
        self.running = True
        self._task = asyncio.create_task(self._run(), name=f"supervised:{self.name}")

    async def _run(self) -> None:
        while self.running:
            try:
                await self.coro_fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "supervised_task_crashed",
                    task=self.name,
                    error=str(e),
                    exc_info=True,
                )
                self._record_restart()
                await asyncio.sleep(self.restart_delay)

    def _record_restart(self) -> None:
        now = time.monotonic()
        self._restart_timestamps.append(now)
        cutoff = now - 600  # 10-minute window
        while self._restart_timestamps and self._restart_timestamps[0] < cutoff:
            self._restart_timestamps.popleft()
        self._restart_count += 1
        if len(self._restart_timestamps) > 10:
            logger.warning(
                "supervised_task_flapping",
                task=self.name,
                restarts_in_10min=len(self._restart_timestamps),
            )

    async def stop(self) -> None:
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    @property
    def restart_count(self) -> int:
        return self._restart_count
