"""Lease heartbeats for the remaining source-specific spider queues."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

logger = logging.getLogger(__name__)

_TABLES = frozenset({
    "youtube_spider_queue",
    "github_spider_queue",
    "strava_spider_queue",
    "lemon8_spider_queue",
})
LEASE_HEARTBEAT_SECONDS = 300


@asynccontextmanager
async def maintain_legacy_spider_lease(
    pool: Any, table: str, row_id: Any, claim_token: Any,
) -> AsyncIterator[None]:
    """Keep one known legacy spider claim fenced until its work finishes."""
    if table not in _TABLES:
        raise ValueError(f"unsupported legacy spider table: {table}")
    stopped = asyncio.Event()

    async def renew() -> None:
        while True:
            try:
                await asyncio.wait_for(stopped.wait(), timeout=LEASE_HEARTBEAT_SECONDS)
                return
            except asyncio.TimeoutError:
                pass
            try:
                async with pool.acquire() as conn:
                    result = await conn.execute(
                        f"UPDATE {table} SET lease_expires_at = NOW() + INTERVAL '30 minutes' "
                        "WHERE id = $1 AND status = 'processing' AND claim_token = $2",
                        row_id,
                        claim_token,
                    )
                if str(result).endswith(" 0"):
                    logger.warning("legacy spider lease lost table=%s row=%s", table, row_id)
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("legacy spider lease renewal failed table=%s row=%s", table, row_id, exc_info=True)

    task = asyncio.create_task(renew())
    try:
        yield
    finally:
        stopped.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
