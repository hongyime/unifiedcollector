from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4  # used for correlation_id generation

import httpx

from .config import settings
from .database import database
from .observability import get_logger

logger = get_logger(__name__)


class BackfillManager:
    def __init__(self) -> None:
        self.running = False
        # correlation_id → job_id mapping is persisted in collector.backfill_jobs
        # (via mark_backfill_running). No in-memory dict needed — DB is the source
        # of truth and survives worker restarts.

    async def start(self) -> None:
        self.running = True
        logger.info("collector_backfill_manager_started")
        await self.resume_pending_jobs()

    async def stop(self) -> None:
        self.running = False
        logger.info("collector_backfill_manager_stopped")

    async def resume_pending_jobs(self) -> None:
        jobs = await database.get_backfill_jobs_to_resume()
        for job in jobs:
            await self.request_backfill_batch(job)

    async def request_backfill_batch(self, job) -> None:
        correlation_id = str(uuid4())
        await database.mark_backfill_running(int(job["id"]), correlation_id)

        payload = {
            "chat_jid": job["chat_jid"],
            "oldest_msg_key": job["oldest_msg_key"],
            "oldest_msg_ts": int(job["oldest_msg_ts"] or 0),
            "count": 100,
            "correlation_id": correlation_id,
        }

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(f"{settings.wa_client_url}/backfill-request", json=payload)
                response.raise_for_status()
        except Exception as exc:
            logger.warning("collector_backfill_request_failed", job_id=job["id"], error=str(exc))

        per_request_sleep = max(0.0, 60.0 / max(1, settings.COLLECTOR_BACKFILL_REQ_PER_MIN))
        await asyncio.sleep(per_request_sleep)

    async def apply_on_demand_history_update(self, correlation_id: str | None, messages: list[dict]) -> None:
        if not correlation_id:
            return

        updated = await database.update_backfill_progress_by_correlation(correlation_id, messages)
        if not updated:
            logger.warning("collector_backfill_orphan_correlation", correlation_id=correlation_id)
            return

        if not messages:
            await database.mark_backfill_complete_by_correlation(correlation_id)

    async def pause_for_session(self, session_name: str) -> None:
        await database.pause_backfills_for_session(session_name)


backfill_manager = BackfillManager()
