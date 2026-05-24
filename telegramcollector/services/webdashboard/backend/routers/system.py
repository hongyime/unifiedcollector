from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Any

import asyncpg
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import database

logger = logging.getLogger(__name__)
router = APIRouter()

_clients: list[WebSocket] = []
_broadcast_task: asyncio.Task | None = None


SERVICES = [
    ("postgres",         lambda: _probe_pg()),
    ("collector",        lambda: _probe_table("collector.raw_messages")),
    ("face_recognition", lambda: _probe_table("face_recognition.face_embeddings")),
    ("user_intelligence",lambda: _probe_table("collector.user_sightings")),
    ("link_discovery",   lambda: _probe_table("link_discovery.discovered_links")),
    ("redis",            lambda: _probe_redis()),
]


async def _probe_pg() -> dict:
    t0 = time.monotonic()
    try:
        v = await database.fetchval("SELECT 1")
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "up" if v == 1 else "down", "latency_ms": ms}
    except Exception:
        return {"status": "down", "latency_ms": None}


async def _probe_table(table: str) -> dict:
    t0 = time.monotonic()
    try:
        await database.fetchval(f"SELECT COUNT(*) FROM {table}")
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "up", "latency_ms": ms}
    except Exception:
        return {"status": "down", "latency_ms": None}


async def _probe_redis() -> dict:
    import os
    t0 = time.monotonic()
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(f"redis://{os.environ.get('REDIS_HOST','redis')}:{os.environ.get('REDIS_PORT','6379')}/0")
        await r.ping()
        await r.aclose()
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "up", "latency_ms": ms}
    except Exception:
        return {"status": "down", "latency_ms": None}


async def _collect_health() -> list[dict]:
    results = await asyncio.gather(*[fn() for _, fn in SERVICES], return_exceptions=True)
    out = []
    for (name, _), res in zip(SERVICES, results):
        if isinstance(res, Exception):
            out.append({"service": name, "status": "unknown", "latency_ms": None})
        else:
            out.append({"service": name, **res})
    return out


async def _broadcast_loop():
    while True:
        try:
            health = await _collect_health()
            payload = json.dumps({"services": health})
            dead = []
            for ws in list(_clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                _clients.remove(ws)
        except Exception as e:
            logger.error("broadcast_loop_error: %s", e)
        await asyncio.sleep(5)


def start_broadcast_task():
    global _broadcast_task
    _broadcast_task = asyncio.create_task(_broadcast_loop())


def stop_broadcast_task():
    global _broadcast_task
    if _broadcast_task:
        _broadcast_task.cancel()
        _broadcast_task = None


@router.websocket("/ws/health")
async def ws_health(websocket: WebSocket):
    await websocket.accept()
    _clients.append(websocket)
    try:
        health = await _collect_health()
        await websocket.send_text(json.dumps({"services": health}))
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        if websocket in _clients:
            _clients.remove(websocket)


@router.get("/ping")
async def ping() -> dict[str, Any]:
    return {"status": "ok", "service": "tgc-dashboard"}
