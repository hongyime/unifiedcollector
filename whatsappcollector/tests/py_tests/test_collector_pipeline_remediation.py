from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

COLLECTOR_ROOT = Path(__file__).resolve().parents[2] / "services" / "collector"
if str(COLLECTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_ROOT))


@pytest.mark.asyncio
async def test_rabbitmq_topology_includes_status_queue_binding():
    from collector.processing_queue import RabbitMQBroker

    class FakeQueue:
        def __init__(self, name: str) -> None:
            self.name = name
            self.bindings: list[tuple[object, str]] = []

        async def bind(self, exchange, routing_key: str):
            self.bindings.append((exchange, routing_key))

    class FakeChannel:
        def __init__(self) -> None:
            self.queues: dict[str, FakeQueue] = {}

        async def declare_exchange(self, *args, **kwargs):
            return object()

        async def declare_queue(self, name: str, *args, **kwargs):
            queue = FakeQueue(name)
            self.queues[name] = queue
            return queue

    broker = RabbitMQBroker("amqp://example")
    broker.channel = FakeChannel()
    broker.exchange = object()

    await broker.declare_topology()

    assert "messages.status" in broker.channel.queues
    assert any(binding[1] == "msg.status" for binding in broker.channel.queues["messages.status"].bindings)


@pytest.mark.asyncio
async def test_redis_topology_includes_status_stream():
    from collector.processing_queue import RedisStreamBroker

    class FakeRedis:
        def __init__(self) -> None:
            self.created: list[str] = []

        async def xgroup_create(self, stream: str, *args, **kwargs):
            self.created.append(stream)

    broker = RedisStreamBroker("redis://example")
    broker.redis = FakeRedis()

    await broker.declare_topology()

    assert "messages.status" in broker.redis.created


@pytest.mark.asyncio
async def test_upsert_raw_message_derives_has_media_from_metadata():
    from collector.database import Database

    class FakeConn:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple]] = []

        async def execute(self, query: str, *params):
            self.calls.append((query, params))

    class FakeAcquire:
        def __init__(self, conn: FakeConn) -> None:
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def __init__(self, conn: FakeConn) -> None:
            self.conn = conn

        def acquire(self):
            return FakeAcquire(self.conn)

    db = Database()
    conn = FakeConn()
    db.pool = FakePool(conn)

    await db.upsert_raw_message(
        {
            "message_id": "m1",
            "chat_jid": "123@g.us",
            "message_type": "image",
            "media_metadata": {"mimetype": "image/jpeg"},
        },
        session_name="session_1",
    )

    assert conn.calls, "Expected INSERT execution"
    params = conn.calls[0][1]
    assert params[8] is True  # has_media parameter


@pytest.mark.asyncio
async def test_worker_handle_status_persists_message_and_sighting(monkeypatch):
    from collector import worker as worker_module

    captured: dict[str, object] = {}

    async def fake_upsert_raw_message(payload, session_name: str):
        captured["payload"] = payload
        captured["session_name"] = session_name

    async def fake_upsert_user_sighting(**kwargs):
        captured["sighting"] = kwargs

    monkeypatch.setattr(worker_module.database, "upsert_raw_message", fake_upsert_raw_message)
    monkeypatch.setattr(worker_module.database, "upsert_user_sighting", fake_upsert_user_sighting)
    monkeypatch.setattr(worker_module.settings, "BROKER_TYPE", "rabbitmq")

    class FakeMessage:
        def __init__(self, payload: dict):
            self.body = json.dumps(payload).encode("utf-8")
            self.acked = False
            self.nacked = False

        async def ack(self):
            self.acked = True

        async def nack(self, requeue: bool = False):
            self.nacked = True

    worker = worker_module.Worker()
    message = FakeMessage(
        {
            "message_id": "status-1",
            "sender_jid": "111@s.whatsapp.net",
            "message_type": "status",
            "media_metadata": {"mimetype": "image/jpeg"},
        }
    )

    await worker.handle_status(message)

    assert message.acked is True
    assert message.nacked is False
    assert captured["payload"]["chat_jid"] == "status@broadcast"
    assert captured["session_name"] == "default"
    assert captured["sighting"]["source_message_id"] == "status-1"
