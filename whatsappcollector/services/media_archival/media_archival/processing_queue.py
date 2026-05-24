from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable

from aio_pika import ExchangeType, Message, connect_robust
from aio_pika.abc import AbstractIncomingMessage

from .observability import get_logger

logger = get_logger(__name__)

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover - optional at runtime
    aioredis = None


class RabbitMQBroker:
    def __init__(self, amqp_url: str):
        self.amqp_url = amqp_url
        self.connection = None
        self.channel = None
        self.exchange = None
        self._is_connected = False

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    async def connect(self) -> None:
        self.connection = await connect_robust(self.amqp_url)
        self.channel = await self.connection.channel()
        self.exchange = await self.channel.declare_exchange(
            "whatsapp.events", ExchangeType.TOPIC, durable=True
        )
        self._is_connected = True

    async def declare_topology(self) -> None:
        if not self.channel:
            raise RuntimeError("RabbitMQ channel not initialized")

        dlq_exchange = await self.channel.declare_exchange("dlq.events", ExchangeType.DIRECT, durable=True)
        dlq_queue = await self.channel.declare_queue("dlq.failed", durable=True)
        await dlq_queue.bind(dlq_exchange, routing_key="dlq.failed")

        queue_bindings = {
            "media.download": ["msg.media.*"],
            "media.process": ["media.process"],
            "media.profile_photo": ["profile_photo.process"],
        }

        args = {
            "x-dead-letter-exchange": "dlq.events",
            "x-dead-letter-routing-key": "dlq.failed",
        }

        for queue_name, bindings in queue_bindings.items():
            queue = await self.channel.declare_queue(queue_name, durable=True, arguments=args)
            for binding in bindings:
                await queue.bind(self.exchange, routing_key=binding)

    async def set_qos(self, prefetch_count: int = 10) -> None:
        if self.channel:
            await self.channel.set_qos(prefetch_count=prefetch_count)

    async def consume(
        self,
        queue_name: str,
        handler: Callable[[AbstractIncomingMessage], Awaitable[None]],
    ):
        if not self.channel:
            raise RuntimeError("RabbitMQ channel not initialized")
        queue = await self.channel.get_queue(queue_name)
        return await queue.consume(handler)

    async def publish(self, routing_key: str, payload: dict):
        if not self.exchange:
            raise RuntimeError("RabbitMQ exchange not initialized")
        await self.exchange.publish(
            Message(body=json.dumps(payload).encode("utf-8"), delivery_mode=2),
            routing_key=routing_key,
        )

    async def get_queue_depth(self, queue_name: str) -> int:
        if not self.channel:
            return 0
        queue = await self.channel.get_queue(queue_name)
        return queue.declaration_result.message_count

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
        self._is_connected = False


class RedisStreamBroker:
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.redis = None
        self._is_connected = False
        self._reconnecting = False
        self._on_reconnect_callback = None

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    async def connect(self) -> None:
        if aioredis is None:
            raise RuntimeError("redis package is not installed")
        self.redis = await aioredis.from_url(self.redis_url, decode_responses=True)
        await self.redis.ping()
        self._is_connected = True

    async def _connect_with_retry(self, max_attempts: int = 10) -> None:
        if aioredis is None:
            raise RuntimeError("redis package is not installed")
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            try:
                self.redis = await aioredis.from_url(self.redis_url, decode_responses=True)
                await self.redis.ping()
                self._is_connected = True
                logger.info("redis_connected", url=self.redis_url)
                return
            except Exception as e:
                self._is_connected = False
                wait = min(2 ** attempt, 15)
                logger.warning("redis_connect_retry", attempt=attempt, wait_seconds=wait, error=str(e))
                await asyncio.sleep(wait)
        raise RuntimeError("Redis connection failed after retries")

    async def _reconnect(self) -> None:
        if self._reconnecting:
            return
        self._reconnecting = True
        try:
            await self._connect_with_retry()
            if self._on_reconnect_callback:
                await self._on_reconnect_callback()
        finally:
            self._reconnecting = False

    async def declare_topology(self) -> None:
        if not self.redis:
            raise RuntimeError("Redis client not initialized")
        for stream in [
            "media.download",
            "media.process",
            "media.profile_photo",
            "dlq.failed",
        ]:
            try:
                await self.redis.xgroup_create(stream, "media_archival", id="$", mkstream=True)
            except Exception as exc:
                if "BUSYGROUP" in str(exc):
                    continue
                raise

    async def set_qos(self, prefetch_count: int = 10) -> None:
        _ = prefetch_count

    async def consume(self, queue_name: str, handler):
        consumer_name = f"media-archival-{os.getpid()}"

        async def _loop():
            while True:
                try:
                    records = await self.redis.xreadgroup(
                        groupname="media_archival",
                        consumername=consumer_name,
                        streams={queue_name: ">"},
                        count=1,
                        block=5000,
                    )
                    if not records:
                        continue
                    _, items = records[0]
                    for message_id, fields in items:
                        payload = fields.get("payload")

                        class _Message:
                            body = str(payload or "{}").encode("utf-8")
                            routing_key = fields.get("routing_key", queue_name)

                            async def ack(self_nonlocal):
                                await self.redis.xack(queue_name, "media_archival", message_id)

                            async def nack(self_nonlocal, requeue: bool = False):
                                await self.redis.xack(queue_name, "media_archival", message_id)
                                if not requeue:
                                    await self.redis.xadd("dlq.failed", fields)

                        await handler(_Message())
                except Exception as e:
                    self._is_connected = False
                    logger.warning("redis_stream_consume_error", stream=queue_name, error=str(e))
                    await asyncio.sleep(1)
                    await self._reconnect()

        return asyncio.create_task(_loop())

    async def publish(self, routing_key: str, payload: dict):
        await self.redis.xadd(routing_key, {"payload": json.dumps(payload), "routing_key": routing_key})

    async def get_queue_depth(self, queue_name: str) -> int:
        if not self.redis:
            return 0
        return int(await self.redis.xlen(queue_name))

    async def close(self) -> None:
        if self.redis:
            await self.redis.close()
        self._is_connected = False


class BrokerManager:
    def __init__(self, broker_type: str, rabbitmq_url: str, redis_url: str):
        self.broker_type = broker_type
        self._broker = RabbitMQBroker(rabbitmq_url) if broker_type == "rabbitmq" else RedisStreamBroker(redis_url)
        self._consumer_registry: list[tuple[str, Callable]] = []

    @property
    def is_connected(self) -> bool:
        return self._broker.is_connected

    async def connect(self):
        await self._broker.connect()
        if self.broker_type == "rabbitmq":
            self._broker.connection.reconnect_callbacks.add(self._on_rmq_reconnect)
        elif self.broker_type == "redis":
            self._broker._on_reconnect_callback = self._reregister_consumers

    async def declare_topology(self):
        await self._broker.declare_topology()

    async def set_qos(self, prefetch_count: int = 10):
        await self._broker.set_qos(prefetch_count)

    async def consume(self, queue_name: str, handler):
        self._consumer_registry.append((queue_name, handler))
        return await self._broker.consume(queue_name, handler)

    async def publish(self, routing_key: str, payload: dict):
        await self._broker.publish(routing_key, payload)

    async def get_queue_depth(self, queue_name: str) -> int:
        return await self._broker.get_queue_depth(queue_name)

    async def close(self):
        await self._broker.close()

    async def _reregister_consumers(self) -> None:
        await self.declare_topology()
        for queue_name, handler in self._consumer_registry:
            for attempt in range(1, 4):
                try:
                    await self._broker.consume(queue_name, handler)
                    break
                except Exception as e:
                    logger.error("consumer_reregister_failed", queue=queue_name, attempt=attempt, error=str(e))
                    if attempt < 3:
                        await asyncio.sleep(5)
        registered = [q for q, _ in self._consumer_registry]
        logger.info("broker_consumers_reregistered", queues=registered)

    async def _on_rmq_reconnect(self, connection) -> None:
        logger.info("rabbitmq_reconnected")
        self._broker._is_connected = True
        self._broker.channel = await connection.channel()
        await self._reregister_consumers()
