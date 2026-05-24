from __future__ import annotations

import asyncio
import os
from pathlib import Path

from .config import settings
from .database import database
from .observability import get_logger, jobs_started_total, sends_attempted_total
from .sender import effective_external_hourly_cap, file_sha256, sender

logger = get_logger(__name__)

_MEDIA_ROOT = Path(getattr(settings, 'MEDIA_STORAGE_PATH', '/data/media')).resolve()


def _assert_safe_path(p: Path) -> None:
    """Raise ValueError if path escapes the allowed media root."""
    try:
        resolved = p.resolve()
    except Exception as exc:
        raise ValueError(f"Cannot resolve path: {p}") from exc
    if not str(resolved).startswith(str(_MEDIA_ROOT)):
        raise ValueError(f"Path {p!r} is outside the allowed media root {_MEDIA_ROOT}")


class JobManager:
    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self.running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run_job(self, job) -> None:
        job_id = int(job["id"])
        mode = str(job["mode"]).lower()
        session_name = str(job["session_name"])
        await database.set_job_status(job_id, "running")
        jobs_started_total.labels(mode=mode).inc()

        source_path = str(job["source_path"] or "")
        if not source_path:
            await database.set_job_status(job_id, "failed", error="source_path is required")
            return

        path_obj = Path(source_path)
        try:
            _assert_safe_path(path_obj)
        except ValueError as exc:
            await database.set_job_status(job_id, "failed", error=f"source_path rejected: {exc}")
            return

        # rglob + is_file are blocking — run off the event loop to avoid stalling
        def _collect_files() -> list[str]:
            if path_obj.is_dir():
                return [str(p) for p in path_obj.rglob("*") if p.is_file()]
            if path_obj.is_file():
                return [str(path_obj)]
            return []

        files = await asyncio.to_thread(_collect_files)
        if not files and not path_obj.exists():
            await database.set_job_status(job_id, "failed", error="source_path does not exist")
            return

        if mode == "external":
            if not bool(job["operator_confirmed"]):
                await database.set_job_status(job_id, "failed", error="operator_confirmed must be TRUE")
                return
            targets = await database.list_targets(job_id)
            if len(targets) > settings.BULK_SENDER_MAX_EXTERNAL_TARGETS:
                await database.set_job_status(job_id, "failed", error="too many external targets")
                return
        else:
            if not settings.BULK_SENDER_INTERNAL_TARGET_JID:
                await database.set_job_status(job_id, "failed", error="BULK_SENDER_INTERNAL_TARGET_JID is required")
                return
            targets = [{"id": 0, "chat_jid": settings.BULK_SENDER_INTERNAL_TARGET_JID}]

        for target in targets:
            target_id = int(target["id"])
            target_chat = str(target["chat_jid"])

            for file_path in files:
                digest = await asyncio.to_thread(file_sha256, file_path)
                if await database.has_sent_hash(job_id, target_chat, digest):
                    sends_attempted_total.labels(mode=mode, status="dedup_skipped").inc()
                    continue

                if mode == "external":
                    result = await sender.run_external_send(job, target_chat, file_path)
                else:
                    result = await sender.run_internal_send(job, target_chat, file_path)

                if not result.sent:
                    sends_attempted_total.labels(mode=mode, status="failed").inc()
                    if result.reason == "session_disconnected_cooldown":
                        return
                    if mode == "external":
                        continue
                    continue

                await database.record_sent_item(job_id, target_chat, file_path, digest, result.wa_message_id)
                await database.update_sent_count(job_id, 1)
                sends_attempted_total.labels(mode=mode, status="sent").inc()

            if target_id:
                await database.mark_target_status(target_id, "complete")

        await database.set_job_status(job_id, "complete")

    async def _run_loop(self) -> None:
        while self.running:
            try:
                jobs = await database.list_runnable_jobs()
                if not jobs:
                    await asyncio.sleep(settings.BULK_SENDER_POLL_INTERVAL_SEC)
                    continue
                for job in jobs:
                    await self._run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("bulk_sender_job_loop_failed", error=str(exc))

            await asyncio.sleep(settings.BULK_SENDER_POLL_INTERVAL_SEC)


job_manager = JobManager()
