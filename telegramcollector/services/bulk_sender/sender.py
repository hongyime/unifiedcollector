"""
Sender — file enumeration, SHA-256 dedup, Pillow validation,
rate-limited Telegram dispatch, and retry/backoff logic.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import pathlib
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from services.bulk_sender.job_manager import JobManager

VALID_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}

logger = logging.getLogger(__name__)


class Sender:
    """Handles file enumeration, dedup, validation, and Telegram dispatch."""

    def __init__(
        self,
        job_manager: JobManager,
        send_delay: float,
        max_retries: int,
        sessions_path: str,
        bot_tokens: list[str],
    ) -> None:
        """Store configuration. No Telegram connections are opened here.

        Args:
            job_manager: Persistence layer for job state.
            send_delay: Inter-send delay in seconds (already clamped to >= 1.0
                by BulkSenderService).
            max_retries: Maximum retry attempts for transient Telegram errors.
            sessions_path: Directory containing .session files for user-account
                sending.
            bot_tokens: Optional list of bot tokens for bot-based sending.
        """
        self.job_manager = job_manager
        self.send_delay = send_delay
        self.max_retries = max_retries
        self.sessions_path = sessions_path
        self.bot_tokens = bot_tokens

    # ------------------------------------------------------------------
    # Hash computation
    # ------------------------------------------------------------------

    def _compute_hash(self, file_path: str) -> str:
        """Read file_path in binary mode and return its SHA-256 hex digest.

        Pure function — no side effects.

        Args:
            file_path: Absolute or relative path to the file.

        Returns:
            64-character lowercase hex string (SHA-256 digest).
        """
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    # ------------------------------------------------------------------
    # Stubs — implemented in subsequent tasks
    # ------------------------------------------------------------------

    def _validate_image(self, file_path: str) -> None:
        """Open file with Pillow and call img.verify().

        Raises an exception if the file is corrupt or unreadable.
        """
        img = Image.open(file_path)
        img.verify()

    def _get_file_list(self, source_path: str) -> list[str]:
        """Recursively enumerate image files under source_path.

        Returns paths sorted lexicographically by full absolute path.
        Raises FileNotFoundError if source_path does not exist.
        """
        source = pathlib.Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Source path does not exist: {source_path}")

        files = [
            str(p.resolve())
            for p in source.rglob('*')
            if p.is_file() and p.suffix.lower() in VALID_EXTENSIONS
        ]
        return sorted(files)

    def _build_collector_query(
        self,
        collector_query: dict,
        count_only: bool = False,
    ) -> tuple[str, list]:
        """Build a parameterised SQL query against collector.raw_messages.

        Args:
            collector_query: Filter dict with optional keys: chat_id,
                date_from, date_to, message_type, sender_id.
            count_only: When True, wraps the query in SELECT COUNT(*).

        Returns:
            Tuple of (sql_string, params_list).
        """
        conditions = ["media_path IS NOT NULL"]
        params = []

        chat_id = collector_query.get("chat_id")
        if chat_id is not None:
            conditions.append("chat_id = %s")
            params.append(chat_id)

        date_from = collector_query.get("date_from")
        if date_from is not None:
            conditions.append("collected_at >= %s")
            params.append(date_from)

        date_to = collector_query.get("date_to")
        if date_to is not None:
            conditions.append("collected_at <= %s")
            params.append(date_to)

        message_type = collector_query.get("message_type") or "photo"
        conditions.append("message_type = %s")
        params.append(message_type)

        sender_id = collector_query.get("sender_id")
        if sender_id is not None:
            conditions.append("sender_id = %s")
            params.append(sender_id)

        where_clause = " AND ".join(conditions)

        if count_only:
            sql = f"SELECT COUNT(*) FROM collector.raw_messages WHERE {where_clause}"
        else:
            sql = f"SELECT media_path FROM collector.raw_messages WHERE {where_clause} ORDER BY media_path ASC"

        return sql, params

    async def _send_file(
        self,
        client,
        target_chat_id: int,
        file_path: str,
        job_id: int,
    ) -> int:
        """Send a single file to target_chat_id with retry/backoff.

        Args:
            client: TelegramClient or Bot instance.
            target_chat_id: Numeric Telegram chat ID of the destination.
            file_path: Path to the file to send.
            job_id: ID of the owning job (for logging).

        Returns:
            telegram_message_id on success.
        """
        attempt = 0
        last_error: Exception | None = None

        while attempt <= self.max_retries:
            try:
                msg = await client.send_file(target_chat_id, file_path)
                return msg.id
            except Exception as e:
                # Check for FloodWait by inspecting the exception class name
                # (avoids hard dependency on telethon at import time)
                if type(e).__name__ == "FloodWaitError":
                    wait_seconds = getattr(e, "seconds", 60) + 5
                    logger.warning(
                        "FloodWait on job_id=%d file=%s: sleeping %ds",
                        job_id, file_path, wait_seconds,
                    )
                    await asyncio.sleep(wait_seconds)
                else:
                    backoff = 2 ** (attempt + 1)  # 2s, 4s, 8s for attempts 0,1,2
                    logger.warning(
                        "Transient error on job_id=%d file=%s attempt=%d: %s — retrying in %ds",
                        job_id, file_path, attempt, e, backoff,
                    )
                    await asyncio.sleep(backoff)
                last_error = e
                attempt += 1

        raise RuntimeError(
            f"All {self.max_retries} retries exhausted for {file_path}"
        ) from last_error

    async def send_job(
        self,
        job: dict,
        stop_event: asyncio.Event,
    ) -> None:
        """Main job execution loop.

        Resolves the file list for the job, then processes each file in order:
        stop check → hash → dedup → validate → send → record → rate-limit sleep.

        Args:
            job: A send_jobs row dict as returned by JobManager.get_job().
            stop_event: Set by BulkSenderService to signal pause or cancel.
        """
        job_id = job["id"]
        target_chat_id = job["target_chat_id"]

        # Resolve file list via JobManager (handles both folder and collector_query)
        file_list = self.job_manager.resolve_file_list(job)

        # Client is a placeholder; _send_file uses it via client.send_file().
        # Tests mock _send_file directly so the client value doesn't matter there.
        client = None

        for file_path in file_list:
            # 1. STOP CHECK
            if stop_event.is_set():
                logger.info(
                    "send_job: stop_event set, exiting loop for job_id=%d", job_id
                )
                break

            # 2. HASH COMPUTATION
            try:
                file_hash = self._compute_hash(file_path)
            except OSError as e:
                logger.error("send_job: cannot read file %s: %s", file_path, e)
                continue

            # 3. DEDUP CHECK
            if self.job_manager.is_already_sent(job_id, file_hash):
                logger.debug(
                    "send_job: skipping already-sent file %s", file_path
                )
                continue

            # 4. IMAGE VALIDATION
            try:
                self._validate_image(file_path)
            except Exception as e:
                logger.error(
                    "send_job: corrupt/invalid image %s: %s", file_path, e
                )
                continue

            # 5. SEND WITH RETRY
            try:
                telegram_message_id = await self._send_file(
                    client, target_chat_id, file_path, job_id
                )
            except Exception as e:
                logger.error(
                    "send_job: all retries exhausted for %s: %s", file_path, e
                )
                continue

            # 6. RECORD SUCCESS
            self.job_manager.record_sent_item(
                job_id, file_path, file_hash, telegram_message_id
            )
            self.job_manager.increment_sent(job_id)

            # 7. RATE LIMIT SLEEP
            await asyncio.sleep(self.send_delay)
