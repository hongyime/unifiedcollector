"""Durable rate-limit event recording for operator dashboards."""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def record_rate_limit_event(
    pool,
    *,
    source: str,
    account: str | None = None,
    scope: str | None = None,
    status_code: int | None = 429,
    cooldown_seconds: int | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO rate_limit_events
                    (source, account, scope, status_code, cooldown_seconds, reason, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                """,
                source,
                account,
                scope,
                status_code,
                cooldown_seconds,
                reason,
                json.dumps(metadata or {}, default=str),
            )
    except Exception:
        logger.debug("rate-limit event record failed", exc_info=True)
