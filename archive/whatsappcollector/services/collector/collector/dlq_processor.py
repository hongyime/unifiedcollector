import asyncio

from .observability import get_logger

logger = get_logger(__name__)


class DLQProcessor:
    def __init__(self, broker):
        self.broker = broker
        self.running = False

    async def monitor_depth(self, interval_seconds: int = 60) -> None:
        """Log-only DLQ monitor (explicitly no alert publishing)."""
        self.running = True
        while self.running:
            try:
                depth = await self.broker.get_queue_depth("dlq.failed")
                logger.info("collector_dlq_depth", depth=depth)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive runtime path
                logger.warning("collector_dlq_monitor_error", error=str(exc))
            await asyncio.sleep(interval_seconds)

    async def stop(self) -> None:
        self.running = False
