from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from .backfill_manager import backfill_manager
from .config import settings
from .database import database
from shared.live_config import ConfigOverlay

overlay = ConfigOverlay(settings, "collector", settings.REDIS_URL)
from .dlq_processor import DLQProcessor
from .observability import dlq_depth, get_logger, message_latency, messages_processed
from .processing_queue import BrokerManager
from .session_health import session_health_monitor
from shared.task_supervisor import TaskSupervisor
from shared.circuit_breaker import CircuitBreaker, CircuitOpenError

logger = get_logger(__name__)


class Worker:
    def __init__(self) -> None:
        self.running = False
        self.broker = BrokerManager(settings.BROKER_TYPE, settings.RABBITMQ_URL, settings.REDIS_URL)
        self.dlq_processor = DLQProcessor(self.broker)
        self.consumer_tags = []
        self._supervisors: list[TaskSupervisor] = []
        self._dedup_circuit = CircuitBreaker(name="collector_redis_dedup", failure_threshold=5, recovery_timeout=60.0)

    async def _check_dedup(self, message_id: str, chat_jid: str, queue_name: str) -> bool:
        """Return True if message is a duplicate (should be skipped), False otherwise."""
        if settings.BROKER_TYPE != "redis":
            return False
        redis_client = getattr(self.broker._broker, "redis", None)
        if not redis_client:
            return False
        dedup_key = f"collector:dedup:{message_id}:{chat_jid}"
        try:
            async def _dedup_check():
                return await redis_client.set(dedup_key, "1", ex=overlay.get("COLLECTOR_DEDUP_TTL_SECONDS"), nx=True)
            was_set = await self._dedup_circuit.call(_dedup_check)
            return not was_set
        except CircuitOpenError:
            logger.warning("dedup_circuit_open", queue=queue_name, message_id=message_id)
            return False
        except Exception as exc:
            logger.warning("collector_dedup_check_failed", error=str(exc))
            return False

    @staticmethod
    def _is_oversized(body: bytes) -> bool:
        return len(body) > settings.MAX_PAYLOAD_BYTES

    @staticmethod
    async def _with_latency(queue_name: str, fn: Callable[[], Awaitable[None]]) -> None:
        with message_latency.labels(queue=queue_name).time():
            await fn()

    async def _decode(self, message) -> dict:
        return json.loads(message.body.decode("utf-8"))

    async def _reject_oversized(self, queue_name: str, message) -> bool:
        if self._is_oversized(message.body):
            logger.warning("collector_payload_too_large", queue=queue_name, size=len(message.body))
            messages_processed.labels(queue=queue_name, status="rejected").inc()
            await message.nack(requeue=False)
            return True
        return False

    async def start(self) -> None:
        self.running = True

        await database.connect()
        await database.seed_registry_and_cursors()

        await self.broker.connect()
        await self.broker.declare_topology()
        await self.broker.set_qos(prefetch_count=20)

        from .observability import start_metrics_server
        start_metrics_server(settings.METRICS_PORT, health_check_fn=lambda: {
            "status": "ok" if self.running and self.broker.is_connected else "degraded",
            "worker": "running" if self.running else "stopped",
            "broker": "connected" if self.broker.is_connected else "disconnected"
        })

        await backfill_manager.start()
        await overlay.start_poll_loop()

        self.consumer_tags.append(await self.broker.consume("messages.inbound", self.handle_message))
        self.consumer_tags.append(await self.broker.consume("messages.status", self.handle_status))
        self.consumer_tags.append(await self.broker.consume("messages.history", self.handle_history))
        self.consumer_tags.append(await self.broker.consume("contacts.update", self.handle_contact))
        self.consumer_tags.append(await self.broker.consume("groups.metadata", self.handle_group))
        self.consumer_tags.append(await self.broker.consume("session.events", self.handle_session_event))
        self.consumer_tags.append(await self.broker.consume("calls", self.handle_call))

        supervisors = [
            TaskSupervisor("dlq_monitor", self._dlq_monitor_loop),
            TaskSupervisor("session_health", self._session_health_loop),
            TaskSupervisor("backfill_resume", self._backfill_resume_loop),
        ]
        self._supervisors = supervisors
        for s in supervisors:
            await s.start()

        logger.info("collector_worker_started")

    async def stop(self) -> None:
        self.running = False

        await backfill_manager.stop()
        await self.dlq_processor.stop()

        for s in self._supervisors:
            await s.stop()

        for tag in self.consumer_tags:
            try:
                await tag.cancel()
            except Exception:
                pass

        await self.broker.close()
        await overlay.stop_poll_loop()
        await database.close()
        logger.info("collector_worker_stopped")

    async def _dlq_monitor_loop(self) -> None:
        while self.running:
            try:
                depth = await self.broker.get_queue_depth("dlq.failed")
                dlq_depth.set(depth)
                logger.info("collector_dlq_depth", depth=depth)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("collector_dlq_monitor_failed", error=str(exc))
            await asyncio.sleep(60)

    async def _session_health_loop(self) -> None:
        while self.running:
            try:
                await session_health_monitor.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("collector_session_health_loop_failed", error=str(exc))
            await asyncio.sleep(60)

    async def _backfill_resume_loop(self) -> None:
        while self.running:
            try:
                await backfill_manager.resume_pending_jobs()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("collector_backfill_resume_failed", error=str(exc))
            await asyncio.sleep(overlay.get("COLLECTOR_BACKFILL_POLL_SECONDS"))

    async def _record_user_sighting(self, payload: dict, session_name: str) -> None:
        user_jid = payload.get("sender_jid") or payload.get("sender_lid")
        chat_jid = payload.get("chat_jid")
        message_id = payload.get("message_id")
        if not user_jid or not chat_jid or not message_id:
            return

        sighting_payload = {
            "source_message_id": str(message_id),
            "source_chat_jid": str(chat_jid),
            "session_name": session_name,
            "message_type": payload.get("message_type"),
            "timestamp": payload.get("timestamp"),
        }
        try:
            await database.upsert_user_sighting(
                user_jid=str(user_jid),
                seen_in_chat_jid=str(chat_jid),
                source_message_id=str(message_id),
                source_chat_jid=str(chat_jid),
                session_name=session_name,
                payload=sighting_payload,
            )
        except Exception as exc:
            logger.warning("collector_user_sighting_upsert_failed", error=str(exc), user_jid=str(user_jid))

    async def handle_message(self, message) -> None:
        queue_name = "messages.inbound"

        async def _run():
            if await self._reject_oversized(queue_name, message):
                return

            payload = await self._decode(message)
            message_id = payload.get("message_id")
            chat_jid = payload.get("chat_jid")
            if not message_id or not chat_jid:
                messages_processed.labels(queue=queue_name, status="rejected").inc()
                await message.nack(requeue=False)
                return

            session_name = payload.get("session_name") or payload.get("_metadata", {}).get("session_name") or "default"

            duplicate = await self._check_dedup(message_id, chat_jid, queue_name)

            if duplicate:
                messages_processed.labels(queue=queue_name, status="duplicate").inc()
                await message.ack()
                return

            await database.upsert_raw_message(payload, session_name=session_name)
            await self._record_user_sighting(payload, session_name)
            messages_processed.labels(queue=queue_name, status="success").inc()
            await message.ack()

        try:
            await self._with_latency(queue_name, _run)
        except Exception as exc:
            logger.error("collector_handle_message_failed", error=str(exc))
            messages_processed.labels(queue=queue_name, status="failed").inc()
            await message.nack(requeue=False)

    async def handle_status(self, message) -> None:
        queue_name = "messages.status"

        async def _run():
            if await self._reject_oversized(queue_name, message):
                return

            payload = await self._decode(message)
            message_id = payload.get("message_id")
            chat_jid = payload.get("chat_jid") or "status@broadcast"
            if not message_id:
                messages_processed.labels(queue=queue_name, status="rejected").inc()
                await message.nack(requeue=False)
                return

            payload["chat_jid"] = chat_jid
            payload.setdefault("chat_type", "status")
            payload.setdefault("message_type", "status")

            session_name = payload.get("session_name") or payload.get("_metadata", {}).get("session_name") or "default"

            duplicate = await self._check_dedup(message_id, chat_jid, queue_name)

            if duplicate:
                messages_processed.labels(queue=queue_name, status="duplicate").inc()
                await message.ack()
                return

            await database.upsert_raw_message(payload, session_name=session_name)
            await self._record_user_sighting(payload, session_name)
            messages_processed.labels(queue=queue_name, status="success").inc()
            await message.ack()

        try:
            await self._with_latency(queue_name, _run)
        except Exception as exc:
            logger.error("collector_handle_status_failed", error=str(exc))
            messages_processed.labels(queue=queue_name, status="failed").inc()
            await message.nack(requeue=False)

    async def handle_history(self, message) -> None:
        queue_name = "messages.history"

        async def _run():
            if await self._reject_oversized(queue_name, message):
                return

            payload = await self._decode(message)
            sync_type = str(payload.get("sync_type") or "").upper()
            correlation_id = payload.get("correlation_id")
            session_name = payload.get("session_name") or "default"
            entries = payload.get("messages") or []

            for entry in entries:
                key = entry.get("key") or {}
                normalized = {
                    "message_id": key.get("id") or entry.get("message_id"),
                    "chat_jid": key.get("remoteJid") or entry.get("chat_jid"),
                    "sender_jid": key.get("participant") or entry.get("sender_jid"),
                    "timestamp": entry.get("messageTimestamp") or entry.get("timestamp"),
                    "message_type": entry.get("message_type") or "history",
                    "body": entry.get("body"),
                    "raw_payload": entry,
                }
                await database.upsert_raw_message(normalized, session_name=session_name)

            if sync_type == "ON_DEMAND":
                await backfill_manager.apply_on_demand_history_update(correlation_id, entries)

            messages_processed.labels(queue=queue_name, status="success").inc()
            await message.ack()

        try:
            await self._with_latency(queue_name, _run)
        except Exception as exc:
            logger.error("collector_handle_history_failed", error=str(exc))
            messages_processed.labels(queue=queue_name, status="failed").inc()
            await message.nack(requeue=False)

    async def handle_contact(self, message) -> None:
        queue_name = "contacts.update"

        async def _run():
            if await self._reject_oversized(queue_name, message):
                return

            payload = await self._decode(message)
            session_name = payload.get("session_name") or payload.get("_metadata", {}).get("session_name") or "default"
            await database.upsert_user(payload)
            await database.upsert_jid_lid_map(payload, session_name=session_name)
            messages_processed.labels(queue=queue_name, status="success").inc()
            await message.ack()

        try:
            await self._with_latency(queue_name, _run)
        except Exception as exc:
            logger.error("collector_handle_contact_failed", error=str(exc))
            messages_processed.labels(queue=queue_name, status="failed").inc()
            await message.nack(requeue=False)

    async def handle_group(self, message) -> None:
        queue_name = "groups.metadata"

        async def _run():
            if await self._reject_oversized(queue_name, message):
                return

            payload = await self._decode(message)
            routing_key = getattr(message, "routing_key", "") or ""

            if routing_key == "groups.participants.update":
                chat_jid = payload.get("id")
                participants = []
                for p in payload.get("participants", []):
                    if isinstance(p, str):
                        participants.append({"id": p, "role": "member"})
                    elif isinstance(p, dict):
                        jid = p.get("id") or ""
                        role = "admin" if p.get("admin") else "member"
                        if jid:
                            participants.append({"id": jid, "role": role})
                if chat_jid and participants:
                    await database.upsert_group_participants(chat_jid, participants)
            else:
                await database.upsert_chat(payload)

            messages_processed.labels(queue=queue_name, status="success").inc()
            await message.ack()

        try:
            await self._with_latency(queue_name, _run)
        except Exception as exc:
            logger.error("collector_handle_group_failed", error=str(exc))
            messages_processed.labels(queue=queue_name, status="failed").inc()
            await message.nack(requeue=False)

    async def handle_session_event(self, message) -> None:
        queue_name = "session.events"

        async def _run():
            if await self._reject_oversized(queue_name, message):
                return

            payload = await self._decode(message)
            await database.upsert_wa_session(payload)
            await database.insert_session_event(payload)

            event_type = str(payload.get("event_type") or "").lower()
            if event_type in {"disconnected", "disconnect"}:
                session_name = payload.get("session_name")
                if session_name:
                    await backfill_manager.pause_for_session(session_name)

            if event_type == "findings_hub_configured":
                jid = payload.get("jid")
                if jid:
                    await database.upsert_system_config("findings_hub_jid", str(jid))
                    logger.info("findings_hub_jid_persisted", jid=jid)

            messages_processed.labels(queue=queue_name, status="success").inc()
            await message.ack()

        try:
            await self._with_latency(queue_name, _run)
        except Exception as exc:
            logger.error("collector_handle_session_event_failed", error=str(exc))
            messages_processed.labels(queue=queue_name, status="failed").inc()
            await message.nack(requeue=False)

    async def handle_call(self, message) -> None:
        queue_name = "calls"

        async def _run():
            if await self._reject_oversized(queue_name, message):
                return

            payload = await self._decode(message)
            await database.insert_call(payload)
            messages_processed.labels(queue=queue_name, status="success").inc()
            await message.ack()

        try:
            await self._with_latency(queue_name, _run)
        except Exception as exc:
            logger.error("collector_handle_call_failed", error=str(exc))
            messages_processed.labels(queue=queue_name, status="failed").inc()
            await message.nack(requeue=False)


worker = Worker()
