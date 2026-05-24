from __future__ import annotations

import asyncio
import json

from .change_tracker import change_tracker
from .config import settings
from .database import database
from .membership_tracker import membership_tracker
from .network_builder import network_builder
from .observability import (
    connections_updates_total,
    cursor_position_gauge,
    get_logger,
    history_changes_total,
    memberships_upsert_total,
    sightings_processed_total,
    start_metrics_server,
)
from shared.live_config import ConfigOverlay

overlay = ConfigOverlay(settings, "user_intelligence", settings.REDIS_URL)

logger = get_logger(__name__)


class UserIntelligenceWorker:
    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self.running = True
        await database.connect()
        await database.seed_cursor()
        start_metrics_server(settings.USER_INTEL_PROMETHEUS_PORT)
        await overlay.start_poll_loop()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("user_intelligence_worker_started")

    async def stop(self) -> None:
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await overlay.stop_poll_loop()
        await database.close()
        logger.info("user_intelligence_worker_stopped")

    async def _process_sighting(self, sighting) -> None:
        payload = sighting["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        payload = payload if isinstance(payload, dict) else {}

        user_jid = str(sighting["user_jid"])
        chat_jid = str(sighting["seen_in_chat_jid"] or "")

        async with database.pool.acquire() as conn:  # type: ignore[union-attr]
            async with conn.transaction():
                last_known = await database.get_last_known_fields(user_jid, conn)
                changes = change_tracker.detect_changes(payload, last_known)
                for field_name, old_value, new_value in changes:
                    await database.insert_user_history(user_jid, field_name, old_value, new_value, conn)
                    history_changes_total.labels(field_name=field_name).inc()

                if chat_jid:
                    is_new_membership = await membership_tracker.record_membership(user_jid, chat_jid, conn)
                    memberships_upsert_total.inc()
                    if is_new_membership:
                        updates = await network_builder.update_for_new_membership(user_jid, chat_jid, conn)
                        if updates:
                            connections_updates_total.inc(updates)

                await database.advance_cursor(int(sighting["id"]), conn=conn)
                cursor_position_gauge.set(int(sighting["id"]))

        sightings_processed_total.inc()

    async def _run_loop(self) -> None:
        while self.running:
            try:
                cursor = await database.get_cursor()
                rows = await database.list_sightings(cursor, overlay.get("USER_INTEL_BATCH_SIZE"))
                if not rows:
                    await asyncio.sleep(overlay.get("USER_INTEL_POLL_INTERVAL_SEC"))
                    continue

                for row in rows:
                    await self._process_sighting(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("user_intelligence_loop_failed", error=str(exc))

            await asyncio.sleep(overlay.get("USER_INTEL_POLL_INTERVAL_SEC"))


worker = UserIntelligenceWorker()
