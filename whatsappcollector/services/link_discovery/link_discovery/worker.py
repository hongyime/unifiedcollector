from __future__ import annotations

import asyncio
import json

from .config import settings
from .database import database
from .extractor import extract_links
from .observability import (
    cursor_position_gauge,
    get_logger,
    links_deduplicated_total,
    links_discovered_total,
    rules_matched_total,
    start_metrics_server,
)
from .queue_rules import select_matching_rule
from shared.live_config import ConfigOverlay

overlay = ConfigOverlay(settings, "link_discovery", settings.REDIS_URL)

logger = get_logger(__name__)


def _extract_text(row) -> str:
    parts: list[str] = []
    body = row.get("body") if hasattr(row, "get") else row["body"]
    if body:
        parts.append(str(body))

    raw_payload = row.get("raw_payload") if hasattr(row, "get") else row["raw_payload"]
    if isinstance(raw_payload, str):
        try:
            raw_payload = json.loads(raw_payload)
        except Exception:
            raw_payload = {}

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            parts.append(value)

    walk(raw_payload)
    return "\n".join(parts)


class LinkDiscoveryWorker:
    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self.running = True
        await database.connect()
        await database.seed_cursor()
        start_metrics_server(settings.LINK_DISCOVERY_PROMETHEUS_PORT)
        await overlay.start_poll_loop()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("link_discovery_worker_started")

    async def stop(self) -> None:
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await overlay.stop_poll_loop()
        await database.close()
        logger.info("link_discovery_worker_stopped")

    async def _process_row(self, row) -> None:
        text = _extract_text(row)
        links = extract_links(text)

        async with database.pool.acquire() as conn:  # type: ignore[union-attr]
            async with conn.transaction():
                rules = await database.list_active_rules(conn)
                for link, link_type in links:
                    inserted = await database.insert_discovered_link(int(row["id"]), link, link_type, conn)
                    if not inserted:
                        links_deduplicated_total.inc()
                        continue

                    links_discovered_total.labels(link_type=link_type).inc()

                    rule = select_matching_rule(rules, f"{link}\n{text}")
                    if rule and rule.auto_queue:
                        await database.enqueue_join(link, source="auto_rule", conn=conn, session_name=rule.preferred_session)
                        rules_matched_total.labels(rule_id=str(rule.id)).inc()

                await database.advance_cursor(int(row["id"]), conn=conn)
                cursor_position_gauge.set(int(row["id"]))

    async def _run_loop(self) -> None:
        while self.running:
            try:
                cursor = await database.get_cursor()
                rows = await database.list_candidate_messages(cursor, overlay.get("LINK_DISCOVERY_BATCH_SIZE"))
                if not rows:
                    await asyncio.sleep(overlay.get("LINK_DISCOVERY_POLL_INTERVAL_SEC"))
                    continue

                for row in rows:
                    await self._process_row(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("link_discovery_loop_failed", error=str(exc))

            await asyncio.sleep(overlay.get("LINK_DISCOVERY_POLL_INTERVAL_SEC"))


worker = LinkDiscoveryWorker()
