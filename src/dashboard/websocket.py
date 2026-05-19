import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from src.db.connection import get_pool

logger = logging.getLogger(__name__)


class ConnectionManager:

    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()


async def health_ws(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            try:
                pool = await get_pool()
                async with pool.acquire() as conn:
                    cursors = await conn.fetch(
                        "SELECT service, status, last_processed_at "
                        "FROM service_cursors ORDER BY service"
                    )
                    stats = await conn.fetch(
                        """
                        SELECT source, COUNT(*) AS total,
                               MAX(collected_at) AS last_collected
                        FROM media_items GROUP BY source
                        """
                    )
                    dlq_count = await conn.fetchval(
                        "SELECT COUNT(*) FROM dead_letter_queue"
                    )

                await ws.send_json({
                    "type": "health",
                    "collectors": [dict(r) for r in cursors],
                    "media_stats": [dict(r) for r in stats],
                    "dlq_count": dlq_count,
                    "ws_clients": manager.count,
                })
            except Exception as e:
                await ws.send_json({"type": "error", "message": str(e)})

            await asyncio.sleep(5)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)
