"""Collector-side import of analyzer collection priority hints.

Analyzer owns identity evidence and can stage "collect this account sooner"
hints in its own DB. Collector owns scheduling, so this module pulls active
hints into collection_targets without analyzer writing collector tables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import asyncpg

from src.core.proximity import analyzer_database_url

logger = logging.getLogger(__name__)

_LAST_REFRESH = 0.0
_REFRESH_LOCK: asyncio.Lock | None = None

DEFAULT_COLLECTOR_PRIORITY = 6
DEFAULT_MIN_CONFIDENCE = 95.0
DEFAULT_LIMIT = 5000

USERNAME_TARGET_SOURCES = {"github", "instagram", "lemon8", "tiktok"}
ID_TARGET_SOURCES = {"strava", "whatsapp", "youtube"}
UNSUPPORTED_SOURCES = {"threads", "x"}


@dataclass(frozen=True)
class CollectorPriorityTarget:
    source: str
    target_id: str
    target_name: str | None
    priority: int
    metadata: dict[str, Any]


async def _ensure_lock() -> asyncio.Lock:
    global _REFRESH_LOCK
    if _REFRESH_LOCK is None:
        _REFRESH_LOCK = asyncio.Lock()
    return _REFRESH_LOCK


async def refresh_collector_priority_hints(pool, *, force: bool = False) -> dict[str, Any]:
    """Best-effort sync from analyzer.collector_priority_hints."""
    if os.getenv("COLLECTOR_PRIORITY_HINTS_ENABLED", "true").lower() != "true":
        return {"skipped": "disabled"}

    interval = int(os.getenv("COLLECTOR_PRIORITY_HINTS_REFRESH_SECONDS", "900"))
    now = time.monotonic()
    global _LAST_REFRESH
    if not force and _LAST_REFRESH and now - _LAST_REFRESH < interval:
        return {"skipped": "fresh"}

    lock = await _ensure_lock()
    async with lock:
        now = time.monotonic()
        if not force and _LAST_REFRESH and now - _LAST_REFRESH < interval:
            return {"skipped": "fresh"}

        dsn = analyzer_database_url()
        if not dsn:
            return {"skipped": "no_analyzer_dsn"}

        try:
            rows = await _fetch_active_analyzer_hints(dsn)
        except Exception as exc:
            logger.debug("collector priority hint refresh: analyzer fetch failed: %s", exc, exc_info=True)
            return {"error": str(exc)[:300]}

        targets, skipped = build_collector_priority_targets(rows)
        try:
            written = await _upsert_collection_targets(pool, targets)
        except Exception as exc:
            logger.debug("collector priority hint refresh: collector write failed: %s", exc, exc_info=True)
            return {"error": str(exc)[:300], "fetched": len(rows), "targets": len(targets)}

        _LAST_REFRESH = time.monotonic()
        if written or targets:
            logger.info(
                "collector priority hints refreshed: fetched=%d targets=%d written=%d skipped=%s",
                len(rows),
                len(targets),
                written,
                skipped,
            )
        return {"fetched": len(rows), "targets": len(targets), "written": written, "skipped": skipped}


async def _fetch_active_analyzer_hints(dsn: str):
    min_confidence = float(os.getenv("COLLECTOR_PRIORITY_HINTS_MIN_CONFIDENCE", str(DEFAULT_MIN_CONFIDENCE)))
    limit = int(os.getenv("COLLECTOR_PRIORITY_HINTS_LIMIT", str(DEFAULT_LIMIT)))
    conn = await asyncpg.connect(dsn, command_timeout=120)
    try:
        return await conn.fetch(
            """
            SELECT id::text AS id,
                   source,
                   target_id,
                   target_username,
                   priority,
                   confidence,
                   hint_type,
                   entity_id::text AS entity_id,
                   candidate_entity_id::text AS candidate_entity_id,
                   relationship_id::text AS relationship_id,
                   evidence,
                   updated_at
            FROM collector_priority_hints
            WHERE status = 'active'
              AND confidence >= $1
            ORDER BY priority ASC, confidence DESC, updated_at DESC
            LIMIT $2
            """,
            min_confidence,
            limit,
        )
    finally:
        await conn.close()


def build_collector_priority_targets(rows) -> tuple[list[CollectorPriorityTarget], dict[str, int]]:
    targets_by_key: dict[tuple[str, str], CollectorPriorityTarget] = {}
    skipped: dict[str, int] = {}
    for row in rows:
        target, reason = collector_target_for_hint(row)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        assert target is not None
        key = (target.source, target.target_id)
        existing = targets_by_key.get(key)
        if existing is None or _hint_rank(target) > _hint_rank(existing):
            targets_by_key[key] = target
    return list(targets_by_key.values()), dict(sorted(skipped.items()))


def collector_target_for_hint(row) -> tuple[CollectorPriorityTarget | None, str | None]:
    source = _clean(_row_value(row, "source")).lower()
    if not source:
        return None, "missing_source"
    if source in UNSUPPORTED_SOURCES:
        return None, "unsupported_source"
    if len(source) > 20:
        return None, "source_too_long"

    username = _normalize_username(_row_value(row, "target_username"))
    native_id = _clean(_row_value(row, "target_id"))
    if source in USERNAME_TARGET_SOURCES:
        target_id = username
    elif source == "telegram":
        target_id = username or native_id
    elif source in ID_TARGET_SOURCES:
        target_id = native_id
    else:
        return None, "unsupported_source"

    if not target_id:
        return None, "missing_target"
    target_id = _normalize_target_id(source, target_id)
    if len(target_id) > 100:
        return None, "target_too_long"

    priority = int(os.getenv("COLLECTOR_PRIORITY_HINTS_TARGET_PRIORITY", str(DEFAULT_COLLECTOR_PRIORITY)))
    confidence = float(_row_value(row, "confidence") or 0.0)
    metadata = {
        "analyzer_priority_hint": {
            "hint_id": _row_value(row, "id"),
            "hint_type": _row_value(row, "hint_type"),
            "confidence": confidence,
            "analyzer_priority": _row_value(row, "priority"),
            "entity_id": _row_value(row, "entity_id"),
            "candidate_entity_id": _row_value(row, "candidate_entity_id"),
            "relationship_id": _row_value(row, "relationship_id"),
            "source_target_id": native_id,
            "source_target_username": username,
            "evidence": _row_value(row, "evidence") or {},
            "updated_at": _string_or_none(_row_value(row, "updated_at")),
        }
    }
    return CollectorPriorityTarget(
        source=source,
        target_id=target_id,
        target_name=username or native_id,
        priority=priority,
        metadata=metadata,
    ), None


async def _upsert_collection_targets(pool, targets: list[CollectorPriorityTarget]) -> int:
    if not targets:
        return 0
    records = [
        (
            target.source,
            target.target_id,
            target.target_name,
            target.priority,
            json.dumps(target.metadata, default=str),
        )
        for target in targets
    ]
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO collection_targets (
                source, target_id, target_name, target_type, status, priority, metadata
            )
            VALUES ($1, $2, $3, 'user', 'pending', $4, $5::jsonb)
            ON CONFLICT (source, target_id) DO UPDATE SET
                target_name = COALESCE(collection_targets.target_name, EXCLUDED.target_name),
                priority = GREATEST(collection_targets.priority, EXCLUDED.priority),
                metadata = COALESCE(collection_targets.metadata, '{}'::jsonb)
                    || jsonb_build_object(
                        'analyzer_priority_hint',
                        EXCLUDED.metadata->'analyzer_priority_hint'
                    )
            """,
            records,
        )
    return len(records)


def _hint_rank(target: CollectorPriorityTarget) -> tuple[int, float]:
    hint = target.metadata.get("analyzer_priority_hint") or {}
    return (target.priority, float(hint.get("confidence") or 0.0))


def _normalize_username(value: Any) -> str | None:
    text = _clean(value)
    if not text:
        return None
    return text.lstrip("@").lower()


def _normalize_target_id(source: str, target_id: str) -> str:
    if source in USERNAME_TARGET_SOURCES or source in {"telegram", "whatsapp"}:
        return target_id.lower()
    return target_id


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return row[key]
