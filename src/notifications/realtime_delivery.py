"""Best-effort delivery ledger for realtime Telegram media posts."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _json(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)


def _sqlstate(exc: BaseException) -> str | None:
    return str(getattr(exc, "sqlstate", "") or "") or None


def _table_missing(exc: BaseException) -> bool:
    return _sqlstate(exc) in {"42P01", "42703"}


def _target_name(target: str | None) -> str | None:
    if not target:
        return None
    try:
        return Path(str(target)).name or str(target)
    except Exception:
        return str(target)


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


async def record_with_conn(
    conn,
    *,
    source: str,
    content_id: str,
    status: str,
    reason: str | None = None,
    file_size: int | None = None,
    content_type: str | None = None,
    dedupe_key: str | None = None,
    telegram_result: dict[str, Any] | None = None,
    target: str | None = None,
) -> None:
    """Upsert one source occurrence into the delivery ledger.

    This is deliberately best-effort. Missing migrations, lock timeouts, and
    transient DB errors must never break collection or Telegram delivery.
    """
    source = str(source or "").strip().lower()
    content_id = str(content_id or "").strip()
    if not source or not content_id:
        return
    status = str(status or "stored_only").strip().lower() or "stored_only"
    try:
        await conn.execute(
            """
            INSERT INTO realtime_media_deliveries (
                media_item_id, source, content_id, status, reason,
                file_size, content_type, dedupe_key, telegram_result, target_name,
                queued_at, sent_at, updated_at
            )
            VALUES (
                (
                    SELECT id
                    FROM media_items
                    WHERE source = $1 AND content_id = $2
                    ORDER BY collected_at DESC
                    LIMIT 1
                ),
                $1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9,
                CASE WHEN $3 = 'enqueued' THEN NOW() ELSE NULL END,
                CASE WHEN $3 IN ('delivered', 'too_large') THEN NOW() ELSE NULL END,
                NOW()
            )
            ON CONFLICT (source, content_id) DO UPDATE SET
                media_item_id = COALESCE(EXCLUDED.media_item_id, realtime_media_deliveries.media_item_id),
                status = EXCLUDED.status,
                reason = EXCLUDED.reason,
                file_size = COALESCE(EXCLUDED.file_size, realtime_media_deliveries.file_size),
                content_type = COALESCE(EXCLUDED.content_type, realtime_media_deliveries.content_type),
                dedupe_key = COALESCE(EXCLUDED.dedupe_key, realtime_media_deliveries.dedupe_key),
                telegram_result = COALESCE(EXCLUDED.telegram_result, realtime_media_deliveries.telegram_result),
                target_name = COALESCE(EXCLUDED.target_name, realtime_media_deliveries.target_name),
                queued_at = COALESCE(realtime_media_deliveries.queued_at, EXCLUDED.queued_at),
                sent_at = CASE
                    WHEN EXCLUDED.sent_at IS NOT NULL THEN EXCLUDED.sent_at
                    ELSE realtime_media_deliveries.sent_at
                END,
                updated_at = NOW()
            """,
            source,
            content_id,
            status,
            reason,
            file_size,
            content_type,
            dedupe_key,
            _json(telegram_result),
            _target_name(target),
        )
    except Exception as exc:  # noqa: BLE001
        if not _table_missing(exc):
            logger.debug(
                "realtime delivery ledger write failed for %s/%s",
                source,
                content_id,
                exc_info=True,
            )


async def record_from_payload(
    payload: dict[str, Any],
    *,
    status: str,
    reason: str | None = None,
    dedupe_key: str | None = None,
    telegram_result: dict[str, Any] | None = None,
    target: str | None = None,
) -> None:
    """Record delivery state from the realtime feed service."""
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        return
    conn = None
    try:
        import asyncpg

        conn = await asyncpg.connect(dsn, timeout=2, command_timeout=5)
        await record_with_conn(
            conn,
            source=str(payload.get("source") or ""),
            content_id=str(payload.get("content_id") or ""),
            status=status,
            reason=reason,
            file_size=_int_or_none(payload.get("file_size")),
            content_type=str(payload.get("content_type") or "") or None,
            dedupe_key=dedupe_key,
            telegram_result=telegram_result,
            target=target,
        )
    except Exception:  # noqa: BLE001
        logger.debug("realtime delivery ledger payload write failed", exc_info=True)
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
