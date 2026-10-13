"""Read-only per-surface feed progress for the Coverage dashboard."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from src.dashboard.api.auth import require_role
from src.db.connection import get_pool

router = APIRouter()


@router.get("/collectors/surface-progress")
async def collection_surface_progress(
    hours: int = Query(default=168, ge=1, le=720),
    _user: dict = Depends(require_role("viewer")),
):
    """Return capture counts and freshness grouped by platform and surface."""
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        events_ready = bool(await conn.fetchval(
            """SELECT EXISTS (
                 SELECT 1 FROM information_schema.columns
                  WHERE table_schema='public' AND table_name='browser_ingest_events'
                    AND column_name='collection_surface'
               )"""
        ))
        media_ready = bool(await conn.fetchval(
            """SELECT EXISTS (
                 SELECT 1 FROM information_schema.columns
                  WHERE table_schema='public' AND table_name='media_items'
                    AND column_name='collection_surface'
               )"""
        ))
        message_ready_count = int(await conn.fetchval(
            """SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name IN ('telegram_messages', 'whatsapp_messages')
                  AND column_name='collection_surface'"""
        ) or 0)
        if not events_ready:
            return {
                "available": False,
                "reason": "collection_surface_migration_pending",
                "hours": hours,
                "generated_at": now.isoformat(),
                "surfaces": [],
            }
        event_rows = await conn.fetch(
            """
            SELECT platform, collection_surface,
                   COUNT(*)::int AS capture_count,
                   COALESCE(SUM(observed_count), 0)::bigint AS observed_count,
                   COALESCE(SUM(stored_count), 0)::bigint AS stored_count,
                   COALESCE(SUM(deduped_count), 0)::bigint AS deduped_count,
                   COALESCE(SUM(failed_count), 0)::bigint AS failed_count,
                   COUNT(*) FILTER (WHERE outcome='failed')::int AS failed_captures,
                   ARRAY_REMOVE(ARRAY_AGG(DISTINCT error_class), NULL) AS error_classes,
                   MAX(created_at) AS last_capture_at,
                   MAX(created_at) FILTER (WHERE outcome IN ('success','partial','empty')) AS last_success_at,
                   MAX(created_at) FILTER (WHERE outcome IN ('failed','partial')) AS last_failure_at
              FROM browser_ingest_events
             WHERE platform <> 'beeper'
               AND created_at >= NOW() - make_interval(hours => $1)
             GROUP BY platform, collection_surface
             ORDER BY platform, collection_surface
            """,
            hours,
        )
        media_rows = []
        if media_ready:
            media_rows = await conn.fetch(
                """
                SELECT source AS platform, collection_surface,
                       COUNT(*)::bigint AS media_rows,
                       MAX(collected_at) AS last_media_at
                  FROM media_items
                 WHERE source <> 'beeper'
                   AND collected_at >= NOW() - make_interval(hours => $1)
                 GROUP BY source, collection_surface
                """,
                hours,
            )
        message_rows = []
        if message_ready_count == 2:
            message_rows = await conn.fetch(
                """
                SELECT platform, collection_surface,
                       COUNT(*)::bigint AS message_rows,
                       MAX(collected_at) AS last_message_at
                  FROM (
                    SELECT 'telegram'::text AS platform, collection_surface, collected_at
                      FROM telegram_messages
                    UNION ALL
                    SELECT 'whatsapp'::text AS platform, collection_surface, collected_at
                      FROM whatsapp_messages
                  ) AS messages
                 WHERE collected_at >= NOW() - make_interval(hours => $1)
                 GROUP BY platform, collection_surface
                """,
                hours,
            )

    surfaces: dict[tuple[str, str], dict] = {}
    for raw in event_rows:
        row = dict(raw)
        key = (str(row.get("platform") or "unknown"), str(row.get("collection_surface") or "unknown"))
        row["platform"], row["surface"] = key
        for name in ("last_capture_at", "last_success_at", "last_failure_at"):
            value = row.get(name)
            row[name] = value.isoformat() if value else None
        surfaces[key] = row

    for raw in media_rows:
        row = dict(raw)
        key = (str(row.get("platform") or "unknown"), str(row.get("collection_surface") or "unknown"))
        target = surfaces.setdefault(key, {
            "platform": key[0], "surface": key[1], "capture_count": 0,
            "observed_count": 0, "stored_count": 0, "deduped_count": 0,
            "failed_count": 0, "failed_captures": 0, "last_capture_at": None,
            "last_success_at": None, "last_failure_at": None,
            "message_rows": 0, "last_message_at": None,
        })
        target["media_rows"] = int(row.get("media_rows") or 0)
        last_media_at = row.get("last_media_at")
        target["last_media_at"] = last_media_at.isoformat() if last_media_at else None
    for raw in message_rows:
        row = dict(raw)
        key = (str(row.get("platform") or "unknown"), str(row.get("collection_surface") or "unknown"))
        target = surfaces.setdefault(key, {
            "platform": key[0], "surface": key[1], "capture_count": 0,
            "observed_count": 0, "stored_count": 0, "deduped_count": 0,
            "failed_count": 0, "failed_captures": 0, "last_capture_at": None,
            "last_success_at": None, "last_failure_at": None,
            "media_rows": 0, "last_media_at": None,
        })
        target["message_rows"] = int(row.get("message_rows") or 0)
        last_message_at = row.get("last_message_at")
        target["last_message_at"] = last_message_at.isoformat() if last_message_at else None
    for row in surfaces.values():
        row.setdefault("media_rows", 0)
        row.setdefault("message_rows", 0)
        row.setdefault("last_media_at", None)
        row.setdefault("last_message_at", None)
        timestamp = row.get("last_success_at") or row.get("last_media_at") or row.get("last_message_at")
        parsed = datetime.fromisoformat(timestamp) if timestamp else None
        if parsed is not None and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        row["freshness_seconds"] = max(0, int((now - parsed).total_seconds())) if parsed else None
        row["capture_path"] = (
            "browser_extension" if int(row.get("capture_count") or 0)
            else "message_stream" if int(row.get("message_rows") or 0)
            else "media_store"
        )

    return {
        "available": True,
        "hours": hours,
        "generated_at": now.isoformat(),
        "surfaces": sorted(surfaces.values(), key=lambda row: (row["platform"], row["surface"])),
    }


__all__ = ["router", "collection_surface_progress"]
