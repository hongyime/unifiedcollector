"""
backend/routers/system.py — System health endpoints and WebSocket broadcaster.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()

# Global set of connected WebSocket clients
_ws_clients: set[WebSocket] = set()
_broadcast_task: asyncio.Task | None = None


async def probe_service(client: httpx.AsyncClient, name: str, base_url: str) -> dict[str, Any]:
    """Probe a single service's health endpoint."""
    # wa-client uses /health; python services use /health at their metrics port
    health_url = f"{base_url}/health"
    start = time.monotonic()
    try:
        resp = await client.get(health_url, timeout=3.0)
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        if resp.status_code < 500:
            return {"service": name, "status": "up", "latency_ms": latency_ms}
        return {"service": name, "status": "down", "latency_ms": latency_ms}
    except Exception as exc:
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        logger.debug("health_probe_failed service=%s err=%s", name, exc)
        return {"service": name, "status": "down", "latency_ms": latency_ms}


async def get_all_health() -> list[dict[str, Any]]:
    """Probe all service health endpoints concurrently."""
    settings = get_settings()
    targets = settings.get_all_service_targets()

    async with httpx.AsyncClient(follow_redirects=False) as client:
        tasks = [probe_service(client, t["name"], t["url"]) for t in targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    health = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            health.append({
                "service": targets[i]["name"],
                "status": "unknown",
                "latency_ms": None,
            })
        else:
            health.append(result)
    return health


@router.get("/health")
async def system_health() -> dict[str, Any]:
    """Return current health status of all services."""
    try:
        services = await get_all_health()
        return {"services": services, "error": None}
    except Exception as exc:
        logger.error("system_health_error: %s", exc)
        return {"services": [], "error": str(exc)}


async def _broadcast_loop() -> None:
    """Background task: broadcast health to all WS clients every 5 seconds."""
    while True:
        await asyncio.sleep(5)
        if not _ws_clients:
            continue
        try:
            health_data = await get_all_health()
            import json
            payload = json.dumps({"services": health_data})
            dead: set[WebSocket] = set()
            for ws in list(_ws_clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.add(ws)
            _ws_clients.difference_update(dead)
        except Exception as exc:
            logger.error("broadcast_loop_error: %s", exc)


def start_broadcast_task() -> None:
    """Start the background broadcast task. Called on app startup."""
    global _broadcast_task
    if _broadcast_task is None or _broadcast_task.done():
        _broadcast_task = asyncio.create_task(_broadcast_loop())
        logger.info("health_broadcast_task_started")


def stop_broadcast_task() -> None:
    """Cancel the broadcast task. Called on app shutdown."""
    global _broadcast_task
    if _broadcast_task and not _broadcast_task.done():
        _broadcast_task.cancel()


async def ws_health(websocket: WebSocket) -> None:
    """WebSocket handler: streams health updates every 5 seconds.
    Registered directly on the app in main.py at /ws/health (not on this router).
    """
    await websocket.accept()
    _ws_clients.add(websocket)
    logger.info("ws_health_client_connected total=%d", len(_ws_clients))
    try:
        # Send immediate snapshot on connect
        health_data = await get_all_health()
        import json
        await websocket.send_text(json.dumps({"services": health_data}))
        # Keep alive, waiting for disconnect
        while True:
            try:
                # Drain any ping/pong or client messages
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        logger.info("ws_health_client_disconnected")
    except Exception as exc:
        logger.debug("ws_health_client_error: %s", exc)
    finally:
        _ws_clients.discard(websocket)
