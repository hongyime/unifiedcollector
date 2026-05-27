from __future__ import annotations

import json

from aio_pika import ExchangeType, Message, connect_robust


class RabbitMQBroker:
    def __init__(self, amqp_url: str):
        self.amqp_url = amqp_url
        self.connection = None
        self.channel = None
        self.exchange = None

    async def connect(self) -> None:
        self.connection = await connect_robust(self.amqp_url)
        self.channel = await self.connection.channel()
        self.exchange = await self.channel.declare_exchange("whatsapp.events", ExchangeType.TOPIC, durable=True)

    async def declare_topology(self) -> None:
        if not self.channel:
            raise RuntimeError("RabbitMQ channel not initialized")

        dlq_exchange = await self.channel.declare_exchange("dlq.events", ExchangeType.DIRECT, durable=True)
        dlq_queue = await self.channel.declare_queue("dlq.failed", durable=True)
        await dlq_queue.bind(dlq_exchange, routing_key="dlq.failed")

        findings_queue = await self.channel.declare_queue(
            "findings.publish",
            durable=True,
            arguments={
                "x-dead-letter-exchange": "dlq.events",
                "x-dead-letter-routing-key": "dlq.failed",
            },
        )
        await findings_queue.bind(self.exchange, routing_key="findings.publish")

    async def publish(self, routing_key: str, payload: dict) -> None:
        if not self.exchange:
            raise RuntimeError("RabbitMQ exchange not initialized")
        await self.exchange.publish(
            Message(body=json.dumps(payload).encode("utf-8"), delivery_mode=2),
            routing_key=routing_key,
        )

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
