"""Bounded replay of stored media rows into the realtime Telegram feed."""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict
from typing import Any, Iterable

from src.notifications import realtime_delivery, realtime_feed


PRIVATE_REALTIME_SOURCES = frozenset({"telegram", "whatsapp", "beeper"})


def parse_sources(raw: str | Iterable[str], *, include_private: bool = False) -> list[str]:
    if isinstance(raw, str):
        values = raw.split(",")
    else:
        values = list(raw)
    sources: list[str] = []
    for value in values:
        source = str(value or "").strip().lower()
        if not source:
            continue
        if source in {"all", "*"}:
            raise ValueError("realtime media backfill requires explicit sources")
        if source in PRIVATE_REALTIME_SOURCES and not include_private:
            raise ValueError(
                f"{source} is a private realtime source; rerun with --include-private to replay it"
            )
        if source not in sources:
            sources.append(source)
    if not sources:
        raise ValueError("at least one source is required")
    return sources


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _json_meta(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _init_counts() -> dict[str, int]:
    return {"selected": 0, "enqueued": 0, "skipped": 0, "stored_only": 0}


def _sample(row: dict[str, Any]) -> dict[str, Any]:
    content_id = str(row.get("content_id") or "")
    return {
        "source": row.get("source"),
        "content_id": content_id[:80],
        "content_type": row.get("content_type"),
        "kind": row.get("kind"),
        "collected_at": row.get("collected_at"),
        "existing_delivery_status": row.get("delivery_status"),
    }


async def _delivery_table_exists(conn) -> bool:
    try:
        return bool(await conn.fetchval("SELECT to_regclass('public.realtime_media_deliveries')"))
    except Exception:
        return False


async def fetch_candidates(
    conn,
    *,
    sources: list[str],
    since_hours: int,
    limit: int,
    per_source_limit: int,
    include_profiles: bool = False,
    include_existing: bool = False,
) -> list[dict[str, Any]]:
    since_hours = max(1, int(since_hours or 1))
    limit = max(1, int(limit or 1))
    per_source_limit = max(1, int(per_source_limit or 1))
    has_delivery_table = await _delivery_table_exists(conn)
    delivery_status_expr = "d.status"
    delivery_join = (
        "LEFT JOIN realtime_media_deliveries d "
        "ON d.source = m.source AND d.content_id = m.content_id"
    )
    existing_filter = "AND ($4::bool OR d.source IS NULL)"
    if not has_delivery_table:
        delivery_status_expr = "NULL::text"
        delivery_join = ""
        existing_filter = "AND ($4::bool OR TRUE)"
    rows = await conn.fetch(
        f"""
        WITH candidate AS (
            SELECT
                m.source,
                m.entity_name,
                m.content_id,
                m.file_path,
                m.source_url,
                m.sha256,
                m.metadata,
                m.kind,
                m.content_type,
                m.file_size,
                m.collected_at,
                {delivery_status_expr} AS delivery_status,
                ROW_NUMBER() OVER (
                    PARTITION BY m.source
                    ORDER BY m.collected_at DESC NULLS LAST, m.content_id DESC
                ) AS source_rank
            FROM media_items m
            {delivery_join}
            WHERE m.source = ANY($1::text[])
              AND m.collected_at >= NOW() - make_interval(hours => $2::int)
              AND ($3::bool OR COALESCE(m.content_type, '') <> 'profile_photo')
              {existing_filter}
        )
        SELECT *
        FROM candidate
        WHERE source_rank <= $5::int
        ORDER BY collected_at DESC NULLS LAST, source, content_id
        LIMIT $6::int
        """,
        sources,
        since_hours,
        include_profiles,
        include_existing,
        per_source_limit,
        limit,
    )
    return [_row_dict(row) for row in rows]


async def _open_redis_client():
    return await realtime_feed._redis_client()  # noqa: SLF001 - shared env parsing/client setup.


async def _enqueue_payload(client, payload: dict[str, Any]) -> None:
    await client.rpush(realtime_feed._queue_key(), json.dumps(payload, default=str))  # noqa: SLF001


async def run_realtime_media_backfill(
    conn,
    *,
    sources: list[str],
    since_hours: int = 36,
    limit: int = 12,
    per_source_limit: int = 4,
    include_profiles: bool = False,
    include_existing: bool = False,
    include_private: bool = False,
    dry_run: bool = True,
    sleep_seconds: float = 1.0,
) -> dict[str, Any]:
    sources = parse_sources(sources, include_private=include_private)
    candidates = await fetch_candidates(
        conn,
        sources=sources,
        since_hours=since_hours,
        limit=limit,
        per_source_limit=per_source_limit,
        include_profiles=include_profiles,
        include_existing=include_existing,
    )
    by_source: dict[str, dict[str, int]] = defaultdict(_init_counts)
    for row in candidates:
        by_source[str(row.get("source") or "unknown")]["selected"] += 1
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "sources": sources,
        "since_hours": since_hours,
        "limit": limit,
        "per_source_limit": per_source_limit,
        "include_profiles": include_profiles,
        "include_existing": include_existing,
        "selected": len(candidates),
        "enqueued": 0,
        "skipped": 0,
        "stored_only": 0,
        "by_source": by_source,
        "samples": [_sample(row) for row in candidates[:20]],
    }
    if dry_run or not candidates:
        report["by_source"] = dict(by_source)
        return report
    if not realtime_feed._flag("REALTIME_POST_FEED_ENABLED", "1"):  # noqa: SLF001
        for row in candidates:
            await realtime_delivery.record_with_conn(
                conn,
                source=str(row.get("source") or ""),
                content_id=str(row.get("content_id") or ""),
                status="stored_only",
                reason="realtime_feed_disabled",
                file_size=row.get("file_size"),
                content_type=row.get("content_type"),
            )
            source_counts = by_source[str(row.get("source") or "unknown")]
            source_counts["stored_only"] += 1
            report["stored_only"] += 1
        report["by_source"] = dict(by_source)
        return report
    client = await _open_redis_client()
    if client is None:
        for row in candidates:
            await realtime_delivery.record_with_conn(
                conn,
                source=str(row.get("source") or ""),
                content_id=str(row.get("content_id") or ""),
                status="stored_only",
                reason="redis_unavailable",
                file_size=row.get("file_size"),
                content_type=row.get("content_type"),
            )
            source_counts = by_source[str(row.get("source") or "unknown")]
            source_counts["stored_only"] += 1
            report["stored_only"] += 1
        report["by_source"] = dict(by_source)
        return report
    try:
        for row in candidates:
            payload = realtime_feed.build_payload(
                source=str(row.get("source") or ""),
                entity_name=str(row.get("entity_name") or ""),
                content_id=str(row.get("content_id") or ""),
                file_path=row.get("file_path"),
                source_url=row.get("source_url"),
                sha256=row.get("sha256"),
                metadata=_json_meta(row.get("metadata")),
                kind=row.get("kind"),
                content_type=row.get("content_type"),
                file_size=row.get("file_size"),
            )
            source_counts = by_source[str(row.get("source") or "unknown")]
            skip_reason = realtime_feed._skip_reason(payload)  # noqa: SLF001
            if skip_reason:
                await realtime_delivery.record_with_conn(
                    conn,
                    source=payload["source"],
                    content_id=payload["content_id"],
                    status="skipped",
                    reason=skip_reason,
                    file_size=row.get("file_size"),
                    content_type=row.get("content_type"),
                )
                source_counts["skipped"] += 1
                report["skipped"] += 1
                continue
            await _enqueue_payload(client, payload)
            await realtime_delivery.record_with_conn(
                conn,
                source=payload["source"],
                content_id=payload["content_id"],
                status="enqueued",
                reason=None,
                file_size=row.get("file_size"),
                content_type=row.get("content_type"),
            )
            source_counts["enqueued"] += 1
            report["enqueued"] += 1
            if sleep_seconds > 0:
                await asyncio.sleep(max(0.0, sleep_seconds))
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()
    report["by_source"] = dict(by_source)
    return report
