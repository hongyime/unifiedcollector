"""BackfillMixin — cursor-based historical pagination for Telegram chats.

Extracted from :mod:`src.collectors.telegram` (PERF-004 sub-plan 4C step 4).
Mixed into :class:`TelegramCollector` via multiple inheritance. All ``self.*``
references resolve at MRO-time to the composed class, so cross-mixin state
still routes through the collector instance unchanged.

Ported from ``telegramcollector/services/collector/backfill_worker.py``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.collectors.telegram.helpers import _is_flood_wait

if TYPE_CHECKING:  # pragma: no cover — forward-ref hints only
    from src.collectors.telegram.session import TelegramWorker


logger = logging.getLogger("src.collectors.telegram")


class BackfillMixin:
    """Cursor-based historical backfill of messages in a Telegram chat."""

    async def backfill_chat(
        self,
        chat_id,
        target_depth: int | None = None,
        max_iterations: int = 10000,
        worker: "TelegramWorker | None" = None,
    ):
        """Cursor-based historical backfill of messages in a chat.

        Walks newest -> oldest via Telethon ``iter_messages`` with ``max_id``
        pagination. Each batch of <=batch_size is persisted before advancing
        the cursor; FloodWait is absorbed via _handle_flood_wait. Bounded by
        ``target_depth`` (stop after N messages persisted) and
        ``max_iterations`` (safety: stop after M batches even if Telegram
        keeps streaming).

        Returns the count of messages written.
        """
        # Auto-spawn a worker if not given one.
        if worker is None:
            if not self._workers:
                self._workers = await self._spawn_workers()
            if not self._workers:
                logger.error("backfill_chat: no Telegram workers — bailing")
                return 0
            worker = self._workers[0]

        client = worker.client
        try:
            entity = await client.get_entity(int(chat_id))
        except (ValueError, TypeError):
            entity = await client.get_entity(chat_id)
        await self._upsert_chat(entity)

        chat_id_int = int(getattr(entity, "id"))
        chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or str(chat_id_int)
        batch_size = self._batch_size
        max_id = 0  # 0 means "from newest"
        written = 0
        iterations = 0

        logger.info(
            "backfill_chat: chat_id=%s name=%s target_depth=%s max_iter=%d",
            chat_id_int, chat_name, target_depth, max_iterations,
        )

        while iterations < max_iterations:
            if self._stop.is_set():
                break
            iterations += 1

            try:
                messages = []
                async for msg in client.iter_messages(
                    entity, limit=batch_size,
                    max_id=max_id if max_id > 0 else 0,
                    reverse=False,
                ):
                    messages.append(msg)
            except Exception as exc:
                if _is_flood_wait(exc):
                    await self._handle_flood_wait(worker, exc)
                    continue
                logger.error("backfill_chat: fetch failed: %s", exc)
                break

            if not messages:
                logger.info("backfill_chat: chat=%s reached end (no more messages)", chat_id_int)
                break

            for message in messages:
                try:
                    sender_uuid = None
                    if getattr(message, "sender_id", None):
                        sender_uuid = await self._upsert_sender(worker, message.sender_id)
                    await self._upsert_message(message, str(chat_id_int), sender_uuid, worker=worker)
                    written += 1
                except Exception as exc:
                    logger.warning(
                        "backfill_chat: failed write chat=%s msg=%s: %s",
                        chat_id_int, getattr(message, "id", "?"), exc,
                    )

            # Advance cursor — min ID in this batch is the next max_id.
            batch_ids = [m.id for m in messages if hasattr(m, "id")]
            if batch_ids:
                max_id = min(batch_ids)

            if target_depth is not None and written >= target_depth:
                logger.info(
                    "backfill_chat: chat=%s hit target_depth=%d (written=%d)",
                    chat_id_int, target_depth, written,
                )
                break

            if len(messages) < batch_size:
                # Partial batch → end of channel.
                break

        logger.info(
            "backfill_chat: chat=%s complete written=%d iterations=%d",
            chat_id_int, written, iterations,
        )
        return written
