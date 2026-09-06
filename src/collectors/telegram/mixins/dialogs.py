"""DialogsMixin — dialog enumeration + telegram_chats upsert.

Extracted from :mod:`src.collectors.telegram` (PERF-004 sub-plan 4C step 6).
Mixed into :class:`TelegramCollector` via multiple inheritance. All ``self.*``
references resolve at MRO-time to the composed class.

Cherry-picked from ``telegramtoolkit/src/core/scan_targets.py`` (iter_dialogs).
"""
from __future__ import annotations

import logging


logger = logging.getLogger("src.collectors.telegram")


class DialogsMixin:
    """Enumerate joined dialogs and upsert them into ``telegram_chats``."""

    async def collect_dialogs(self) -> list[dict]:
        """Enumerate joined dialogs across all workers and upsert telegram_chats.

        Returns a deduplicated list of {platform_chat_id, title, type} dicts.
        Workers running in parallel will see the same shared chats; we
        dedupe by platform_chat_id so we only INSERT each one once.
        """
        if not self._workers:
            self._workers = await self._spawn_workers()
        if not self._workers:
            logger.error("collect_dialogs: no Telegram workers — bailing")
            return []

        seen: dict[str, dict] = {}
        for worker in self._workers:
            if self._stop.is_set():
                break
            try:
                async for dialog in worker.client.iter_dialogs():
                    entity = getattr(dialog, "entity", None)
                    if entity is None:
                        continue
                    cid = str(getattr(entity, "id", ""))
                    if not cid or cid in seen:
                        continue
                    # Upsert into telegram_chats.
                    try:
                        await self._upsert_chat(entity)
                    except Exception as exc:
                        logger.debug("upsert_chat failed for %s: %s", cid, exc)
                    if getattr(entity, "broadcast", False):
                        chat_type = "channel"
                    elif getattr(entity, "megagroup", False):
                        chat_type = "supergroup"
                    elif hasattr(entity, "title"):
                        chat_type = "group"
                    else:
                        chat_type = "private"
                    seen[cid] = {
                        "platform_chat_id": cid,
                        "title": getattr(entity, "title", None)
                                 or getattr(entity, "username", None)
                                 or cid,
                        "type": chat_type,
                    }
            except Exception as exc:
                logger.error(
                    "[worker=%d account=%s] collect_dialogs failed: %s",
                    worker.worker_id, worker.account.name, exc,
                )

        logger.info("collect_dialogs: %d unique dialog(s)", len(seen))
        return list(seen.values())
