"""
shared/dlq.py — Dead-letter queue helpers shared across all collector services.

Provides:
    nack_to_dlq(message, reason)  — nack a message with a reason header, no requeue
    DLQConsumerBase               — base class for services that monitor dlq.failed
"""
import json

import aio_pika

from shared.observability import get_logger

logger = get_logger("dlq")

DLQ_QUEUE_NAME = "dlq.failed"


async def nack_to_dlq(message: aio_pika.IncomingMessage, reason: str) -> None:
    """Nack a broker message without requeue.  Logs the reason for observability.

    The broker routes rejected messages to dlq.failed via the dead-letter
    exchange configured on each queue.
    """
    logger.warning(
        "message_nacked_to_dlq",
        queue=message.routing_key,
        reason=reason,
        message_id=message.message_id,
    )
    await message.nack(requeue=False)


class DLQConsumerBase:
    """Base class for DLQ monitor tasks.

    Subclass this and override `handle_dlq_message` to implement
    service-specific DLQ handling logic.

    Intentionally does NOT call any broker publisher inside the monitor — that
    would cascade if the broker is the source of the problem (fixes BUG-09).
    """

    dlq_name: str = DLQ_QUEUE_NAME

    async def handle_dlq_message(self, payload: dict, raw_message: aio_pika.IncomingMessage) -> None:
        """Override in subclass.  Called for each message on dlq.failed."""
        raise NotImplementedError

    async def monitor_depth(self, channel: aio_pika.Channel, depth_threshold: int = 50) -> None:
        """Log a warning if DLQ depth exceeds threshold.

        Does NOT publish alerts — if the broker is degraded, publishing would
        fail and generate a secondary exception that buries the root cause.
        """
        try:
            queue = await channel.declare_queue(self.dlq_name, passive=True)
            depth = queue.declaration_result.message_count
            if depth >= depth_threshold:
                logger.warning(
                    "dlq_depth_threshold_exceeded",
                    queue=self.dlq_name,
                    depth=depth,
                    threshold=depth_threshold,
                )
            else:
                logger.debug("dlq_depth_ok", queue=self.dlq_name, depth=depth)
        except Exception as exc:
            logger.error("dlq_depth_check_failed", error=str(exc))

    async def run_consumer(self, channel: aio_pika.Channel) -> None:
        """Consume messages from dlq.failed and call handle_dlq_message for each."""
        queue = await channel.declare_queue(self.dlq_name, durable=True)
        async with queue.iterator() as q_iter:
            async for message in q_iter:
                async with message.process(ignore_processed=True):
                    try:
                        payload = json.loads(message.body.decode("utf-8"))
                        await self.handle_dlq_message(payload, message)
                    except Exception as exc:
                        logger.error(
                            "dlq_handler_error",
                            error=str(exc),
                            routing_key=message.routing_key,
                        )
