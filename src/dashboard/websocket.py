import asyncio
import json
import logging
import os

from fastapi import WebSocket, WebSocketDisconnect

import jwt

from src.db.connection import get_pool

logger = logging.getLogger(__name__)


_JWT_SECRET = os.getenv("DASHBOARD_JWT_SECRET", "")


def _verify_token(token: str | None) -> dict | None:
    if not token or not _JWT_SECRET:
        return None
    try:
        return jwt.decode(token, _JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


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
    # Auth: accept token via ?token=... query param or Sec-WebSocket-Protocol.
    token = ws.query_params.get("token") if hasattr(ws, "query_params") else None
    if token is None:
        try:
            token = ws.headers.get("authorization", "").removeprefix("Bearer ").strip() or None
        except Exception:
            token = None
    payload = _verify_token(token)
    if payload is None:
        await ws.close(code=4401)
        return

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

                    # Matrix collector metrics — added Wave 1 Phase 3.
                    # Tolerate missing tables (collector might not be
                    # deployed) so the broadcaster never falls over.
                    matrix_metrics: dict | None = None
                    if os.getenv("MATRIX_COLLECTOR_ENABLED", "").lower() in (
                        "1", "true", "yes", "on"
                    ):
                        try:
                            undecrypted = await conn.fetchval(
                                "SELECT COUNT(*) FROM matrix_events "
                                "WHERE is_encrypted = TRUE AND is_decrypted = FALSE"
                            )
                            pending_media = await conn.fetchval(
                                "SELECT COUNT(*) FROM matrix_events "
                                "WHERE media_mxc IS NOT NULL "
                                "AND media_local_path IS NULL "
                                "AND (is_encrypted = FALSE OR is_decrypted = TRUE)"
                            )
                            backfill_pending = await conn.fetchval(
                                "SELECT COUNT(*) FROM matrix_backfill_state "
                                "WHERE done = FALSE"
                            )
                            matrix_metrics = {
                                "undecrypted": int(undecrypted or 0),
                                "pending_media": int(pending_media or 0),
                                "backfill_pending": int(backfill_pending or 0),
                            }
                        except Exception as me:
                            logger.debug("matrix metrics tick skipped: %s", me)
                            matrix_metrics = None

                await ws.send_json({
                    "type": "health",
                    "collectors": [dict(r) for r in cursors],
                    "media_stats": [dict(r) for r in stats],
                    "dlq_count": dlq_count,
                    "ws_clients": manager.count,
                    "matrix": matrix_metrics,
                })
            except Exception as e:
                # Don't leak internal exception text to clients.
                logger.exception("health_ws tick error: %s", e)
                await ws.send_json({"type": "error", "message": "internal error"})

            await asyncio.sleep(5)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)
