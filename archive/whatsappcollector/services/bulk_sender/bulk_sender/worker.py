from __future__ import annotations

import asyncio

from .config import settings
from .database import database
from .job_manager import job_manager
from .observability import get_logger, job_status_gauge, start_metrics_server
from shared.task_supervisor import TaskSupervisor
from shared.live_config import ConfigOverlay

overlay = ConfigOverlay(settings, "bulk_sender", settings.REDIS_URL)

logger = get_logger(__name__)


class BulkSenderWorker:
    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None
        self._supervisors: list[TaskSupervisor] = []

    async def start(self) -> None:
        self.running = True
        await database.connect()
        start_metrics_server(settings.BULK_SENDER_PROMETHEUS_PORT)
        await job_manager.start()
        await overlay.start_poll_loop()
        supervisor = TaskSupervisor("job_manager_loop", job_manager._run_loop)
        self._supervisors = [supervisor]
        await supervisor.start()
        self._task = asyncio.create_task(self._metrics_loop())
        logger.info("bulk_sender_worker_started")

    async def stop(self) -> None:
        self.running = False
        for s in self._supervisors:
            await s.stop()
        await job_manager.stop()
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await overlay.stop_poll_loop()
        await database.close()
        logger.info("bulk_sender_worker_stopped")

    async def _metrics_loop(self) -> None:
        while self.running:
            try:
                stats = await database.summary_stats()
                job_status_gauge.set(stats["running"])
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(15)


worker = BulkSenderWorker()
