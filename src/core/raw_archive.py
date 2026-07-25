"""Helpers for durable raw payload archive failure reporting."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from src.core.vault import RawPayloadResult

logger = logging.getLogger(__name__)


_ENTITY_KEYS = (
    "platform_chat_id",
    "platform_user_id",
    "platform_athlete_id",
    "platform_activity_id",
    "username",
    "chat_id",
    "account_id",
    "collection_account",
    "session_name",
    "owner",
)


def _short(value: Any, limit: int = 100) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit]


def raw_archive_entity_id(source: str, metadata: Mapping[str, Any] | None) -> str:
    metadata = metadata or {}
    for key in _ENTITY_KEYS:
        value = metadata.get(key)
        if value:
            return _short(value)
    return _short(source)


def raw_archive_content_id(artifact_id: str) -> str:
    normalized = str(artifact_id or "unknown").replace("\\", "/")
    return _short(f"raw:{normalized}")


async def insert_raw_archive_failure(
    pool: Any,
    *,
    source: str,
    artifact_id: str,
    error: str | None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if pool is None:
        logger.warning(
            "%s raw archive failed for %s but no DB pool was available: %s",
            source,
            artifact_id,
            error,
        )
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
            VALUES ($1, $2, $3, $4)
            """,
            _short(source, 20),
            raw_archive_entity_id(source, metadata),
            raw_archive_content_id(artifact_id),
            f"raw payload archive failed for {artifact_id}: {error}"[:8000],
        )


def report_raw_archive_result(
    pool: Any,
    *,
    source: str,
    artifact_id: str,
    result: RawPayloadResult | None,
    metadata: Mapping[str, Any] | None = None,
    log: logging.Logger | None = None,
    error: str | None = None,
) -> None:
    if result is not None and result.ok:
        return
    error = error or (result.error if result is not None else "raw payload write returned no result")
    log = log or logger
    log.warning("%s raw archive failed for %s: %s", source, artifact_id, error)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning(
            "%s raw archive failure could not be queued outside an event loop: %s",
            source,
            artifact_id,
        )
        return
    loop.create_task(
        insert_raw_archive_failure(
            pool,
            source=source,
            artifact_id=artifact_id,
            error=error,
            metadata=metadata,
        )
    )
