from __future__ import annotations

import asyncio
import contextlib
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings
from .observability import (
    findings_published_total,
    findings_queued_total,
    findings_skipped_total,
    get_logger,
    publisher_queue_depth,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class FindingItem:
    identity_id: str
    original_image_path: str
    event_type: str
    confidence: float
    caption: str | None = None
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = field(default_factory=dict)


class FindingsPublisher:
    def __init__(self) -> None:
        self._broker: Any | None = None
        self._queue: deque[FindingItem] = deque()
        self._lock = asyncio.Lock()
        self._running = False
        self._task: asyncio.Task | None = None
        self._tokens = float(settings.FINDINGS_MAX_PER_HOUR)
        self._last_refill = datetime.now(timezone.utc)

    def start(self, broker: Any) -> None:
        self._broker = broker
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("face_findings_publisher_started")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.flush_once()
        logger.info("face_findings_publisher_stopped")

    def _refill_tokens(self) -> None:
        now = datetime.now(timezone.utc)
        elapsed = (now - self._last_refill).total_seconds()
        self._last_refill = now
        if elapsed <= 0:
            return
        rate_per_second = settings.FINDINGS_MAX_PER_HOUR / 3600.0
        self._tokens = min(float(settings.FINDINGS_MAX_PER_HOUR), self._tokens + elapsed * rate_per_second)

    def _jitter_delay(self) -> float:
        base = max(0.0, float(settings.FINDINGS_SEND_DELAY))
        return random.uniform(base * 0.8, base * 1.2)

    async def publish_sighting(
        self,
        *,
        identity_id: str,
        original_image_path: str,
        event_type: str,
        confidence: float,
        caption: str | None = None,
    ) -> None:
        if float(confidence or 0.0) < settings.FINDINGS_MIN_CONFIDENCE:
            findings_skipped_total.inc()
            logger.debug("face_finding_skipped_low_confidence", identity_id=identity_id, confidence=confidence)
            return

        item = FindingItem(
            identity_id=str(identity_id),
            original_image_path=original_image_path,
            event_type=event_type,
            confidence=float(confidence),
            caption=caption,
            payload={
                "identity_id": str(identity_id),
                "original_image_path": original_image_path,
                "event_type": event_type,
                "confidence": float(confidence),
                "caption": caption,
            },
        )
        async with self._lock:
            self._queue.append(item)
            findings_queued_total.inc()
            publisher_queue_depth.set(len(self._queue))

    async def flush_once(self) -> None:
        if not self._broker:
            return

        while True:
            async with self._lock:
                if not self._queue:
                    publisher_queue_depth.set(0)
                    return
                self._refill_tokens()
                if self._tokens < 1.0:
                    publisher_queue_depth.set(len(self._queue))
                    return
                item = self._queue.popleft()
                publisher_queue_depth.set(len(self._queue))
                self._tokens -= 1.0

            try:
                await self._broker.publish(settings.FINDINGS_QUEUE_NAME, item.payload)
                findings_published_total.inc()
                logger.info("face_finding_published", identity_id=item.identity_id)
            except Exception as exc:  # pragma: no cover - runtime protection
                logger.warning("face_finding_publish_failed", identity_id=item.identity_id, error=str(exc))
                async with self._lock:
                    self._queue.appendleft(item)
                    publisher_queue_depth.set(len(self._queue))
                return

            await asyncio.sleep(self._jitter_delay())

    async def _run(self) -> None:
        while self._running:
            try:
                await self.flush_once()
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("face_findings_publisher_loop_failed", error=str(exc))
                await asyncio.sleep(1.0)


findings_publisher = FindingsPublisher()
