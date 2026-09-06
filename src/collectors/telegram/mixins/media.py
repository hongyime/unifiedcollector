"""MediaMixin — single-message media download.

Extracted from :mod:`src.collectors.telegram` (PERF-004 sub-plan 4C step 9).
Mixed into :class:`TelegramCollector` via multiple inheritance; every
``self.*`` reference resolves at MRO-time to the composed class.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.collectors.telegram.helpers import _telegram_message_content_id

if TYPE_CHECKING:  # pragma: no cover — forward-ref hints only
    from src.collectors.telegram.session import TelegramWorker


logger = logging.getLogger("src.collectors.telegram")


class MediaMixin:
    """Single-message media download routed through :meth:`download_media`."""

    async def download_message_media(
        self,
        message_or_id,
        worker: "TelegramWorker | None" = None,
        chat_id=None,
    ):
        """Download the media attached to a Telethon message.

        Accepts either a Telethon Message object directly, or
        (message_id, chat_id) so callers without an event handle can
        re-fetch. Routes through self.download_media (which performs
        atomic write + sha256 + insert_media_item) — that is the unified
        delegated-backend equivalent of src/core/media_download.py for
        Telethon's library-level download_media() API.
        """
        if worker is None:
            if not self._workers:
                self._workers = await self._spawn_workers()
            if not self._workers:
                logger.error("download_message_media: no workers — bailing")
                return None
            worker = self._workers[0]

        client = worker.client

        # Resolve message object if only an ID was passed.
        message = message_or_id
        if not hasattr(message, "media"):
            if chat_id is None:
                logger.error("download_message_media: chat_id required when given an ID")
                return None
            try:
                msgs = await client.get_messages(int(chat_id), ids=int(message_or_id))
                message = msgs if hasattr(msgs, "media") else (msgs[0] if msgs else None)
            except Exception as exc:
                logger.warning(
                    "download_message_media: get_messages failed for chat=%s message=%s: %s",
                    chat_id,
                    message_or_id,
                    exc,
                )
                return None

        if message is None or getattr(message, "media", None) is None:
            return None

        chat_id_str = str(chat_id) if chat_id is not None else str(getattr(message, "chat_id", "unknown"))
        # Try to resolve a name; fall back to chat_id.
        chat_name = chat_id_str
        chat_username: str | None = None
        try:
            entity = await client.get_entity(int(chat_id_str))
            chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or chat_id_str
            chat_username = getattr(entity, "username", None)
        except Exception:
            pass

        from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
        if isinstance(message.media, MessageMediaPhoto):
            await self._handle_photo(worker, message, chat_id_str, chat_name, chat_username)
            return True
        if isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            mime = getattr(doc, "mime_type", "") or ""
            await self._handle_document(worker, message, chat_id_str, chat_name, mime, chat_username)
            return True

        # Unknown media type — fall through to a generic Telethon download.
        try:
            data = await client.download_media(message.media, bytes)
            if not data:
                return None
            _, _, ext = self._extract_file_info(message)
            await self.download_media({
                "entity_id": chat_id_str,
                "entity_name": chat_name,
                "content_type": "media",
                "content_id": _telegram_message_content_id(chat_id_str, message.id),
                "data": data,
                "extension": ext or "bin",
                "raw": message.to_dict(),
                "chat_username": chat_username,
                "message_id": message.id,
            }, worker=worker)
            return True
        except Exception as exc:
            logger.error("download_message_media generic path failed: %s", exc)
            return None
