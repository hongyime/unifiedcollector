"""Read-only per-surface feed progress for the Coverage dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.dashboard.api.auth import require_role
from src.db.connection import get_pool
from src.core.collection_surface import COLLECTION_SURFACES, classify_collection_error

router = APIRouter()

FEED_CONTROL_PLATFORMS = (
    "facebook", "github", "instagram", "lemon8", "strava",
    "telegram", "threads", "tiktok", "x", "youtube",
)


class FeedControlUpdate(BaseModel):
    enabled: bool
    interval_hours: int = Field(default=24, ge=1, le=720)


async def _fetch_rows_safely(conn, query: str, *args, timeout: float) -> tuple[list, str | None]:
    try:
        return list(await conn.fetch(query, *args, timeout=timeout)), None
    except Exception as exc:  # noqa: BLE001 — report only a safe error category
        return [], classify_collection_error(exc.__class__.__name__)


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
                "freshness_available": False,
                "freshness_error_class": "schema_unavailable",
                "reason": "collection_surface_migration_pending",
                "hours": hours,
                "generated_at": now.isoformat(),
                "surfaces": [],
            }
        event_rows, event_window_error = await _fetch_rows_safely(conn,
            """
            SELECT platform, collection_surface,
                   COUNT(*)::int AS capture_count,
                   COALESCE(SUM(observed_count), 0)::bigint AS observed_count,
                   COALESCE(SUM(stored_count), 0)::bigint AS stored_count,
                   COALESCE(SUM(deduped_count), 0)::bigint AS deduped_count,
                   COALESCE(SUM(failed_count), 0)::bigint AS failed_count,
                   COUNT(*) FILTER (WHERE outcome='failed')::int AS failed_captures
              FROM browser_ingest_events
             WHERE platform = ANY($2::text[])
               AND created_at >= NOW() - make_interval(hours => $1)
             GROUP BY platform, collection_surface
             ORDER BY platform, collection_surface
            """,
            hours, list(FEED_CONTROL_PLATFORMS),
            timeout=3.0,
        )
        event_freshness_rows, event_freshness_error = await _fetch_rows_safely(conn,
            """
            SELECT platform.platform, surface.surface,
                   latest_capture.created_at AS last_capture_at,
                   latest_success.created_at AS last_success_at,
                   latest_failure.created_at AS last_failure_at
              FROM unnest($1::text[]) AS platform(platform)
              CROSS JOIN unnest($2::text[]) AS surface(surface)
              LEFT JOIN LATERAL (
                  SELECT event.created_at FROM browser_ingest_events AS event
                   WHERE event.platform=platform.platform
                     AND event.collection_surface=surface.surface
                   ORDER BY event.created_at DESC LIMIT 1
              ) AS latest_capture ON TRUE
              LEFT JOIN LATERAL (
                  SELECT event.created_at FROM browser_ingest_events AS event
                   WHERE event.platform=platform.platform
                     AND event.collection_surface=surface.surface
                     AND event.outcome IN ('success','partial','empty')
                   ORDER BY event.created_at DESC LIMIT 1
              ) AS latest_success ON TRUE
              LEFT JOIN LATERAL (
                  SELECT event.created_at FROM browser_ingest_events AS event
                   WHERE event.platform=platform.platform
                     AND event.collection_surface=surface.surface
                     AND event.outcome IN ('failed','partial')
                   ORDER BY event.created_at DESC LIMIT 1
              ) AS latest_failure ON TRUE
             WHERE latest_capture.created_at IS NOT NULL
                OR latest_success.created_at IS NOT NULL
                OR latest_failure.created_at IS NOT NULL
            """,
            list(FEED_CONTROL_PLATFORMS), list(COLLECTION_SURFACES),
            timeout=2.5,
        )
        media_rows = []
        media_freshness_rows = []
        media_window_error = None
        media_freshness_error = None
        if media_ready:
            media_rows, media_window_error = await _fetch_rows_safely(conn,
                """
                SELECT source AS platform, collection_surface,
                       COUNT(*)::bigint AS media_rows,
                       MAX(collected_at) AS window_last_media_at
                  FROM media_items
                 WHERE source = ANY($2::text[])
                   AND collected_at >= NOW() - make_interval(hours => $1)
                 GROUP BY source, collection_surface
                """,
                hours, list(FEED_CONTROL_PLATFORMS),
                timeout=3.0,
            )
            media_freshness_rows, media_freshness_error = await _fetch_rows_safely(conn,
                """
                SELECT platform.platform, surface.surface,
                       latest_media.collected_at AS last_media_at
                  FROM unnest($1::text[]) AS platform(platform)
                  CROSS JOIN unnest($2::text[]) AS surface(surface)
                  LEFT JOIN LATERAL (
                      SELECT item.collected_at FROM media_items AS item
                       WHERE item.source=platform.platform
                         AND item.collection_surface=surface.surface
                       ORDER BY item.collected_at DESC LIMIT 1
                  ) AS latest_media ON TRUE
                 WHERE latest_media.collected_at IS NOT NULL
                """,
                list(FEED_CONTROL_PLATFORMS), list(COLLECTION_SURFACES),
                timeout=2.5,
            )
        message_rows = []
        message_freshness_rows = []
        message_window_error = None
        message_freshness_error = None
        if message_ready_count == 2:
            message_rows, message_window_error = await _fetch_rows_safely(conn,
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
                timeout=3.0,
            )
            message_freshness_rows, message_freshness_error = await _fetch_rows_safely(conn,
                """
                SELECT platform, collection_surface,
                       MAX(collected_at) AS last_message_at
                  FROM (
                    SELECT 'telegram'::text AS platform, collection_surface, collected_at
                      FROM telegram_messages
                    UNION ALL
                    SELECT 'whatsapp'::text AS platform, collection_surface, collected_at
                      FROM whatsapp_messages
                  ) AS messages
                 GROUP BY platform, collection_surface
                """,
                timeout=2.5,
            )

    freshness_errors = (
        event_window_error, event_freshness_error, media_window_error,
        media_freshness_error, message_window_error, message_freshness_error,
    )
    freshness_error_class = next((item for item in freshness_errors if item), None)

    surfaces: dict[tuple[str, str], dict] = {}
    for raw in event_freshness_rows:
        row = dict(raw)
        key = (str(row.get("platform") or "unknown"), str(row.get("surface") or "unknown"))
        surfaces[key] = {
            "platform": key[0], "surface": key[1], "capture_count": 0,
            "observed_count": 0, "stored_count": 0, "deduped_count": 0,
            "failed_count": 0, "failed_captures": 0,
            "last_capture_at": row["last_capture_at"].isoformat() if row.get("last_capture_at") else None,
            "last_success_at": row["last_success_at"].isoformat() if row.get("last_success_at") else None,
            "last_failure_at": row["last_failure_at"].isoformat() if row.get("last_failure_at") else None,
        }
    for raw in event_rows:
        row = dict(raw)
        key = (str(row.get("platform") or "unknown"), str(row.get("collection_surface") or "unknown"))
        target = surfaces.setdefault(key, {
            "platform": key[0], "surface": key[1], "last_capture_at": None,
            "last_success_at": None, "last_failure_at": None,
        })
        target.update(row)
        target["platform"], target["surface"] = key

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
    for raw in media_freshness_rows:
        row = dict(raw)
        key = (str(row.get("platform") or "unknown"), str(row.get("surface") or "unknown"))
        target = surfaces.setdefault(key, {
            "platform": key[0], "surface": key[1], "capture_count": 0,
            "observed_count": 0, "stored_count": 0, "deduped_count": 0,
            "failed_count": 0, "failed_captures": 0, "last_capture_at": None,
            "last_success_at": None, "last_failure_at": None, "message_rows": 0,
            "last_message_at": None,
        })
        value = row.get("last_media_at")
        target["last_media_at"] = value.isoformat() if value else None
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
    for raw in message_freshness_rows:
        row = dict(raw)
        key = (str(row.get("platform") or "unknown"), str(row.get("collection_surface") or "unknown"))
        target = surfaces.setdefault(key, {
            "platform": key[0], "surface": key[1], "capture_count": 0,
            "observed_count": 0, "stored_count": 0, "deduped_count": 0,
            "failed_count": 0, "failed_captures": 0, "last_capture_at": None,
            "last_success_at": None, "last_failure_at": None, "media_rows": 0,
            "last_media_at": None,
        })
        value = row.get("last_message_at")
        target["last_message_at"] = value.isoformat() if value else None
    for row in surfaces.values():
        for name in ("last_capture_at", "last_success_at", "last_failure_at", "last_media_at", "last_message_at"):
            value = row.get(name)
            if value and not isinstance(value, str):
                row[name] = value.isoformat()
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
        "freshness_available": freshness_error_class is None,
        "freshness_error_class": freshness_error_class,
        "hours": hours,
        "generated_at": now.isoformat(),
        "surfaces": sorted(surfaces.values(), key=lambda row: (row["platform"], row["surface"])),
    }


@router.get("/collectors/operations")
async def collector_operations(
    hours: int = Query(default=168, ge=1, le=720),
    _user: dict = Depends(require_role("viewer")),
):
    """Return bounded shared-queue age, recent error classes, and feed controls."""
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    controls = {
        platform: {
            "platform": platform,
            "enabled": True,
            "interval_hours": 24,
            "next_run": None,
            "configured": False,
        }
        for platform in FEED_CONTROL_PLATFORMS
    }
    queues = {
        platform: {
            "platform": platform,
            "status_counts": {},
            "pending_count": 0,
            "in_progress_count": 0,
            "failed_count": 0,
            "oldest_pending_at": None,
            "oldest_pending_age_seconds": None,
            "oldest_in_progress_at": None,
            "oldest_in_progress_age_seconds": None,
            "age_basis": "spider_queue.enqueued_at",
        }
        for platform in FEED_CONTROL_PLATFORMS
    }
    error_rows: list[dict] = []
    error_sources = {
        source: {"source": source, "available": False, "error_class": None}
        for source in ("browser_capture", "spider_graph", "headless_dlq")
    }
    queue_age_available = False
    queue_error_class = None

    async with pool.acquire() as conn:
        schedules_ready = bool(await conn.fetchval(
            "SELECT to_regclass('public.collection_schedules') IS NOT NULL"
        ))
        if schedules_ready:
            schedule_rows = await conn.fetch(
                """SELECT source, enabled, interval_hours, next_run
                     FROM collection_schedules
                    WHERE source = ANY($1::text[])""",
                list(FEED_CONTROL_PLATFORMS),
            )
            for raw in schedule_rows:
                row = dict(raw)
                platform = str(row.get("source") or "").lower()
                if platform not in controls:
                    continue
                next_run = row.get("next_run")
                controls[platform] = {
                    "platform": platform,
                    "enabled": bool(row.get("enabled")),
                    "interval_hours": int(row.get("interval_hours") or 24),
                    "next_run": next_run.isoformat() if next_run else None,
                    "configured": True,
                }

        spider_queue_ready = bool(await conn.fetchval(
            "SELECT to_regclass('public.spider_queue') IS NOT NULL"
        ))
        if spider_queue_ready:
            try:
                rows = await conn.fetch(
                    """SELECT platform, status, COUNT(*)::int AS item_count,
                              MIN(enqueued_at) FILTER (WHERE status='pending') AS oldest_pending_at,
                              MIN(last_attempted_at) FILTER (WHERE status IN ('in_progress','processing'))
                                  AS oldest_in_progress_at
                         FROM spider_queue
                        WHERE platform = ANY($1::text[])
                        GROUP BY platform, status
                        ORDER BY platform, status""",
                    list(FEED_CONTROL_PLATFORMS),
                    timeout=1.5,
                )
                queue_age_available = True
                for raw in rows:
                    row = dict(raw)
                    platform = str(row.get("platform") or "").lower()
                    if platform not in queues:
                        continue
                    target = queues[platform]
                    status = str(row.get("status") or "unknown")
                    count = int(row.get("item_count") or 0)
                    target["status_counts"][status] = count
                    if status == "pending":
                        target["pending_count"] += count
                        target["oldest_pending_at"] = row.get("oldest_pending_at")
                    elif status in {"in_progress", "processing"}:
                        target["in_progress_count"] += count
                        candidate = row.get("oldest_in_progress_at")
                        current = target.get("oldest_in_progress_at")
                        if candidate and (current is None or candidate < current):
                            target["oldest_in_progress_at"] = candidate
                    elif status in {"failed", "unavailable", "unresolvable"}:
                        target["failed_count"] += count
                for target in queues.values():
                    for field, age_field in (
                        ("oldest_pending_at", "oldest_pending_age_seconds"),
                        ("oldest_in_progress_at", "oldest_in_progress_age_seconds"),
                    ):
                        value = target.get(field)
                        if value is not None:
                            if value.tzinfo is None:
                                value = value.replace(tzinfo=timezone.utc)
                            target[field] = value.isoformat()
                            target[age_field] = max(0, int((now - value).total_seconds()))
            except Exception as exc:  # noqa: BLE001 — operations page must degrade safely
                queue_error_class = classify_collection_error(exc.__class__.__name__)

        browser_errors_ready = bool(await conn.fetchval(
            """SELECT EXISTS (
                 SELECT 1 FROM information_schema.columns
                  WHERE table_schema='public' AND table_name='browser_ingest_events'
                    AND column_name='error_class'
               )"""
        ))
        if browser_errors_ready:
            try:
                rows = await conn.fetch(
                    """SELECT platform, error_class, COUNT(*)::int AS error_count,
                              MAX(created_at) AS last_seen_at
                         FROM browser_ingest_events
                        WHERE platform = ANY($2::text[])
                          AND created_at >= NOW() - make_interval(hours => $1)
                          AND error_class IS NOT NULL
                        GROUP BY platform, error_class
                        ORDER BY error_count DESC, last_seen_at DESC
                        LIMIT 50""",
                    hours, list(FEED_CONTROL_PLATFORMS),
                    timeout=1.5,
                )
                error_rows.extend({
                    "platform": str(row["platform"]),
                    "error_class": classify_collection_error(row["error_class"]),
                    "category": classify_collection_error(row["error_class"]),
                    "error_count": int(row["error_count"] or 0),
                    "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
                    "source": "browser_capture",
                } for row in rows)
                error_sources["browser_capture"]["available"] = True
            except Exception as exc:  # noqa: BLE001 — expose status, never the DB message
                error_sources["browser_capture"]["error_class"] = classify_collection_error(exc.__class__.__name__)
        else:
            error_sources["browser_capture"]["error_class"] = "schema_unavailable"

        graph_errors_ready = bool(await conn.fetchval(
            "SELECT to_regclass('public.spider_fetch_observations') IS NOT NULL"
        ))
        if graph_errors_ready:
            try:
                rows = await conn.fetch(
                    """SELECT platform, error_class, COUNT(*)::int AS error_count,
                              MAX(last_attempt_at) AS last_seen_at
                         FROM spider_fetch_observations
                        WHERE platform = ANY($2::text[])
                          AND last_attempt_at >= NOW() - make_interval(hours => $1)
                          AND outcome IN ('unavailable', 'error')
                          AND error_class IS NOT NULL
                        GROUP BY platform, error_class
                        ORDER BY error_count DESC, last_seen_at DESC
                        LIMIT 50""",
                    hours, list(FEED_CONTROL_PLATFORMS),
                    timeout=1.5,
                )
                error_rows.extend({
                    "platform": str(row["platform"]),
                    "error_class": classify_collection_error(row["error_class"]),
                    "category": classify_collection_error(row["error_class"]),
                    "error_count": int(row["error_count"] or 0),
                    "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
                    "source": "spider_graph",
                } for row in rows)
                error_sources["spider_graph"]["available"] = True
            except Exception as exc:  # noqa: BLE001 — expose status, never the DB message
                error_sources["spider_graph"]["error_class"] = classify_collection_error(exc.__class__.__name__)
        else:
            error_sources["spider_graph"]["error_class"] = "schema_unavailable"

        dlq_ready = bool(await conn.fetchval(
            "SELECT to_regclass('public.dead_letter_queue') IS NOT NULL"
        ))
        if dlq_ready:
            try:
                rows = await conn.fetch(
                    """
                    SELECT source AS platform,
                           CASE
                             WHEN LOWER(COALESCE(error_message, '')) ~ '(429|rate.?limit|ratelimit|throttl)' THEN 'rate_limit'
                             WHEN LOWER(COALESCE(error_message, '')) ~ '(401|auth|login|cookie|credential)' THEN 'auth'
                             WHEN LOWER(COALESCE(error_message, '')) ~ '(403|private|visibility|forbidden|unavailable)' THEN 'visibility'
                             WHEN LOWER(COALESCE(error_message, '')) ~ '(timeout|timedout)' THEN 'timeout'
                             WHEN LOWER(COALESCE(error_message, '')) ~ '(parse|decode|json)' THEN 'parse'
                             WHEN LOWER(COALESCE(error_message, '')) ~ '(database|postgres|sql|persistence)' THEN 'persistence'
                             WHEN LOWER(COALESCE(error_message, '')) ~ '(network|connect|http|socket)' THEN 'network'
                             ELSE 'other'
                           END AS error_class,
                           COUNT(*)::int AS error_count,
                           MAX(created_at) AS last_seen_at
                      FROM dead_letter_queue
                     WHERE source = ANY($2::text[])
                       AND created_at >= NOW() - make_interval(hours => $1)
                     GROUP BY source, error_class
                     ORDER BY error_count DESC, last_seen_at DESC
                     LIMIT 50
                    """,
                    hours, list(FEED_CONTROL_PLATFORMS),
                    timeout=1.5,
                )
                error_rows.extend({
                    "platform": str(row["platform"]),
                    "error_class": str(row["error_class"]),
                    "category": classify_collection_error(row["error_class"]),
                    "error_count": int(row["error_count"] or 0),
                    "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
                    "source": "headless_dlq",
                } for row in rows)
                error_sources["headless_dlq"]["available"] = True
            except Exception as exc:  # noqa: BLE001 — error text stays in the database
                error_sources["headless_dlq"]["error_class"] = classify_collection_error(exc.__class__.__name__)
        else:
            error_sources["headless_dlq"]["error_class"] = "schema_unavailable"

    return {
        "available": True,
        "hours": hours,
        "generated_at": now.isoformat(),
        "queue_age_available": queue_age_available,
        "queue_error_class": queue_error_class,
        "queues": sorted(queues.values(), key=lambda row: row["platform"]),
        "errors": sorted(error_rows, key=lambda row: (-row["error_count"], row["platform"]))[:100],
        "error_sources": sorted(error_sources.values(), key=lambda row: row["source"]),
        "error_reporting_available": any(row["available"] for row in error_sources.values()),
        "error_reporting_partial": any(not row["available"] for row in error_sources.values()),
        "controls": sorted(controls.values(), key=lambda row: row["platform"]),
        "control_interval_bounds_hours": {"min": 1, "max": 720},
    }


@router.put("/collectors/feed-controls/{platform}")
async def update_feed_control(
    platform: str,
    req: FeedControlUpdate,
    _user: dict = Depends(require_role("operator")),
):
    """Pause/resume one in-scope collector and bound its scheduler cadence."""
    platform = platform.strip().lower()
    if platform not in FEED_CONTROL_PLATFORMS:
        raise HTTPException(status_code=404, detail="Unknown feed platform")
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    next_run = now + timedelta(hours=req.interval_hours)
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT to_regclass('public.collection_schedules') IS NOT NULL"):
            raise HTTPException(status_code=503, detail="Collection schedules are unavailable")
        await conn.execute(
            """INSERT INTO collection_schedules (source, interval_hours, enabled, next_run)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (source) DO UPDATE SET
                   interval_hours=EXCLUDED.interval_hours,
                   enabled=EXCLUDED.enabled,
                   next_run=EXCLUDED.next_run""",
            platform, req.interval_hours, req.enabled, next_run,
        )
    return {
        "status": "ok",
        "platform": platform,
        "enabled": req.enabled,
        "interval_hours": req.interval_hours,
        "next_run": next_run.isoformat(),
    }


__all__ = [
    "router",
    "FeedControlUpdate",
    "collection_surface_progress",
    "collector_operations",
    "update_feed_control",
]
