"""Ingestion, backfill-equilibrium, and instagram-health routes.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 19
(cluster 7). Covers:

* ``/api/backfill-equilibrium`` — read-only backfill drain state
* ``/instagram/health`` — operational stuck-point report
* ``/ingestion/hourly`` — hour-by-hour real ingestion from source tables

Cross-module helpers (``_vault_payload``, ``_safe_row``,
``_existing_public_tables``, ``_INGESTION_CONTENT_PARTS``,
``_INGESTION_HOURLY_CACHE``) are looked up at call time to preserve test
monkey-patches.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.dashboard.api.helpers import (
    _acquire_dashboard_conn,
    _release_dashboard_conn,
    _iso_or_none,
)

logger = logging.getLogger(__name__)


def _lookup(name: str):
    root = sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


async def _get_pool():
    fn = _lookup("get_pool") or get_pool
    return await fn()


router = APIRouter()


@router.get("/api/backfill-equilibrium")
async def backfill_equilibrium(_user: dict = Depends(require_role("viewer"))):
    """Read-only backfill drain state for the dashboard.

    Queue tables tell us how far each platform is from historical equilibrium:
    pending/processing rows are backlog; terminal rows are drained work. Beeper
    and Matrix expose explicit backfill-state tables, so include those too.
    """
    pool = await _get_pool()
    generated_at = datetime.now(timezone.utc).isoformat()

    queue_tables = {
        "github": "github_spider_queue",
        "instagram": "instagram_spider_queue",
        "lemon8": "lemon8_spider_queue",
        "strava": "strava_spider_queue",
        "telegram": "telegram_spider_queue",
        "tiktok": "tiktok_spider_queue",
        "youtube": "youtube_spider_queue",
    }
    terminal_statuses = {"completed", "done", "failed", "unresolvable"}
    completed_statuses = {"completed", "done"}
    running_statuses = {"pending", "processing", "in_progress"}

    def pct(num: int, den: int) -> float | None:
        return None if not den else round((num / den) * 100.0, 2)

    async def table_exists(conn, name: str) -> bool:
        return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{name}"))

    async def status_counts(conn, table: str, platform: str) -> dict:
        if not await table_exists(conn, table):
            return {"platform": platform, "queue_table": table, "missing_table": True}
        approximate = False
        row_estimate = int(await conn.fetchval(
            "SELECT COALESCE(reltuples, 0)::bigint FROM pg_class WHERE oid = to_regclass($1)",
            f"public.{table}",
        ) or 0)
        if row_estimate > 500_000:
            # Large queues are usually dominated by pending rows. Exact pending
            # counts seq-scan the table and can hang the dashboard. Count the
            # small non-pending buckets exactly via the status index and infer an
            # estimated pending/total from pg_class.reltuples.
            counts = {}
            for status in ("completed", "done", "failed", "unresolvable", "processing", "in_progress"):
                n = await conn.fetchval(f"SELECT COUNT(*)::int FROM {table} WHERE status = $1", status)
                if n:
                    counts[status] = int(n)
            counts["pending"] = max(row_estimate - sum(counts.values()), 0)
            approximate = True
        else:
            rows = await conn.fetch(f"SELECT status, COUNT(*)::int AS n FROM {table} GROUP BY status")
            counts = {str(r["status"] or "unknown"): int(r["n"] or 0) for r in rows}
        total = sum(counts.values())
        queue_depth = sum(counts.get(s, 0) for s in running_statuses)
        completed = sum(counts.get(s, 0) for s in completed_statuses)
        terminal = sum(counts.get(s, 0) for s in terminal_statuses)
        failed = counts.get("failed", 0) + counts.get("unresolvable", 0)
        return {
            "platform": platform,
            "queue_table": table,
            "status_counts": counts,
            "total": total,
            "pending": counts.get("pending", 0),
            "processing": counts.get("processing", 0) + counts.get("in_progress", 0),
            "queue_depth": queue_depth,
            "completed": completed,
            "failed": failed,
            "terminal": terminal,
            "completed_pct": pct(completed, total),
            "terminal_pct": pct(terminal, total),
            "backfill_complete_pct": pct(terminal, total),
            "approximate": approximate,
        }

    async def target_counts(conn) -> dict[str, dict]:
        if not await table_exists(conn, "collection_targets"):
            return {}
        rows = await conn.fetch(
            "SELECT source, status, COUNT(*)::int AS n "
            "FROM collection_targets GROUP BY source, status"
        )
        out: dict[str, dict] = {}
        for r in rows:
            src = str(r["source"])
            out.setdefault(src, {"target_total": 0, "target_status_counts": {}})
            out[src]["target_status_counts"][str(r["status"] or "unknown")] = int(r["n"] or 0)
            out[src]["target_total"] += int(r["n"] or 0)
        return out

    platforms: dict[str, dict] = {}
    async with pool.acquire() as conn:
        for platform, table in queue_tables.items():
            platforms[platform] = await status_counts(conn, table, platform)

        if await table_exists(conn, "spider_queue"):
            rows = await conn.fetch(
                "SELECT platform, status, COUNT(*)::int AS n "
                "FROM spider_queue GROUP BY platform, status"
            )
            generic: dict[str, dict[str, int]] = {}
            for r in rows:
                generic.setdefault(str(r["platform"]), {})[str(r["status"])] = int(r["n"] or 0)
            for platform, counts in generic.items():
                total = sum(counts.values())
                queue_depth = sum(counts.get(s, 0) for s in running_statuses)
                completed = sum(counts.get(s, 0) for s in completed_statuses)
                terminal = sum(counts.get(s, 0) for s in terminal_statuses)
                platforms.setdefault(platform, {"platform": platform})["generic_spider_queue"] = {
                    "status_counts": counts,
                    "total": total,
                    "queue_depth": queue_depth,
                    "completed": completed,
                    "terminal": terminal,
                    "completed_pct": pct(completed, total),
                    "terminal_pct": pct(terminal, total),
                }

        if await table_exists(conn, "beeper_shadow_sync_state"):
            row = await conn.fetchrow(
                "SELECT COUNT(*)::int AS total, "
                "(COUNT(*) FILTER (WHERE backfill_complete))::int AS complete "
                "FROM beeper_shadow_sync_state"
            )
            total = int(row["total"] or 0)
            complete = int(row["complete"] or 0)
            platforms["beeper"] = {
                "platform": "beeper",
                "queue_table": "beeper_shadow_sync_state",
                "total": total,
                "completed": complete,
                "queue_depth": max(total - complete, 0),
                "backfill_complete": complete,
                "backfill_running": max(total - complete, 0),
                "backfill_complete_pct": pct(complete, total),
            }
        if await table_exists(conn, "matrix_backfill_state"):
            row = await conn.fetchrow(
                "SELECT COUNT(*)::int AS total, "
                "(COUNT(*) FILTER (WHERE done))::int AS complete "
                "FROM matrix_backfill_state"
            )
            total = int(row["total"] or 0)
            complete = int(row["complete"] or 0)
            platforms["matrix"] = {
                "platform": "matrix",
                "queue_table": "matrix_backfill_state",
                "total": total,
                "completed": complete,
                "queue_depth": max(total - complete, 0),
                "backfill_complete": complete,
                "backfill_running": max(total - complete, 0),
                "backfill_complete_pct": pct(complete, total),
            }

        for platform, target in (await target_counts(conn)).items():
            platforms.setdefault(platform, {"platform": platform}).update(target)

    rows = sorted(platforms.values(), key=lambda r: r.get("platform", ""))
    totals = {
        "platforms": len(rows),
        "queue_depth": sum(int(r.get("queue_depth") or 0) for r in rows),
        "completed": sum(int(r.get("completed") or 0) for r in rows),
        "total": sum(int(r.get("total") or 0) for r in rows),
    }
    totals["backfill_complete_pct"] = pct(totals["completed"], totals["total"])
    return {"generated_at": generated_at, "totals": totals, "platforms": rows}


def _derive_instagram_stuck_stage(report: dict) -> str:
    cooldown = report.get("cooldown") or {}
    if cooldown.get("active"):
        return "cooldown"
    if not report.get("latest_browser_ingest"):
        return "login_or_browser"
    targets = report.get("targets") or {}
    pending_targets = int(targets.get("pending") or targets.get("active") or 0)
    if pending_targets <= 0 and int(targets.get("total") or 0) <= 0:
        return "targets"
    if not report.get("latest_profile"):
        return "profile_api"
    if not report.get("latest_post"):
        return "posts"
    if not report.get("latest_media"):
        return "media_download"
    vault = report.get("vault") or {}
    if vault.get("available") is False or vault.get("writable") is False:
        return "vault"
    realtime = report.get("realtime_delivery") or {}
    if realtime.get("available") is False:
        return "realtime_feed"
    status_counts = realtime.get("status_counts") or {}
    delivered = int(status_counts.get("delivered") or status_counts.get("sent") or 0)
    failed = int(status_counts.get("failed") or 0) + int(status_counts.get("too_large") or 0)
    enqueued = int(status_counts.get("enqueued") or status_counts.get("deferred") or 0)
    if delivered <= 0 and (failed > 0 or enqueued > 0):
        return "telegram_upload"
    return "ok"


@router.get("/instagram/health")
async def instagram_health(_user: dict = Depends(require_role("viewer"))):
    """Operational Instagram stuck-point report without raw private bodies."""
    _vault_payload = _lookup("_vault_payload")
    try:
        return await _instagram_health_impl(_user)
    except asyncio.TimeoutError:
        return {
            "source": "instagram",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "degraded": True,
            "error": "timeout",
            "tables": {},
            "section_errors": {"report": "TimeoutError"},
            "targets": {"total": 0},
            "spider_targets": {"total": 0},
            "latest_profile": None,
            "latest_post": None,
            "latest_media": None,
            "browser_ingest_24h": {},
            "revisit_queue": {"available": False},
            "realtime_delivery": {"available": False},
            "cooldown": {"active": False},
            "source_health": None,
            "vault": _vault_payload() if _vault_payload else {},
        }


async def _instagram_health_impl(_user: dict = Depends(require_role("viewer"))):
    """Operational Instagram stuck-point report without raw private bodies."""
    _vault_payload = _lookup("_vault_payload")
    _safe_row = _lookup("_safe_row")
    _existing_public_tables = _lookup("_existing_public_tables")
    pool = await _get_pool()
    report = {
        "source": "instagram",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "tuning": {
            "one_browser_tab": True,
            "story_sweep_cap": {"key": "ucIgStorySweepCap", "default": 6, "min": 5, "max": 8},
            "deep_profile_cap": {"key": "ucIgDeepProfileCap", "default": 3, "min": 2, "max": 5},
            "rest_window_minutes": {"key": "ucIgRestWindowMinutes", "default": 10, "min": 8, "max": 22},
            "browser_revisit_hourly_cap": {"key": "ucIgRevisitHourlyCap", "default": 1, "min": 0, "max": 3},
            "cooldown_minutes_429": {"key": "ucIg429CooldownMinutes", "default": 75, "min": 45, "max": 180},
            "famous_account_skip_cap": {"key": "ucIgFamousFollowerCap", "default": 3000, "min": 1000, "max": 5000},
        },
        "tables": {},
        "section_errors": {},
        "targets": {"total": 0},
        "spider_targets": {"total": 0},
        "latest_profile": None,
        "latest_post": None,
        "latest_media": None,
        "latest_browser_ingest": None,
        "browser_ingest_24h": {},
        "revisit_queue": {"available": False},
        "realtime_delivery": {"available": False},
        "cooldown": {"active": False},
        "source_health": None,
        "vault": _vault_payload() if _vault_payload else {},
    }
    expected_tables = [
        "collection_targets",
        "instagram_spider_targets",
        "instagram_profiles",
        "instagram_posts",
        "media_items",
        "browser_ingest_events",
        "browser_media_revisit_queue",
        "realtime_media_deliveries",
        "rate_limit_events",
        "source_health",
    ]
    async with pool.acquire() as conn:
        tables = await _existing_public_tables(conn, expected_tables, timeout=3)
        report["tables"] = {name: name in tables for name in expected_tables}

        if "collection_targets" in tables:
            rows = await conn.fetch(
                """
                SELECT COALESCE(status, 'unknown') AS status, count(*)::int AS count
                FROM collection_targets
                WHERE source = 'instagram'
                GROUP BY status
                """,
                timeout=3,
            )
            counts = {str(row["status"]): int(row["count"] or 0) for row in rows}
            counts["total"] = sum(counts.values())
            report["targets"] = counts

        if "instagram_spider_targets" in tables:
            rows = await conn.fetch(
                """
                SELECT COALESCE(status, 'unknown') AS status, count(*)::int AS count
                FROM instagram_spider_targets
                GROUP BY status
                """,
                timeout=3,
            )
            counts = {str(row["status"]): int(row["count"] or 0) for row in rows}
            counts["total"] = sum(counts.values())
            report["spider_targets"] = counts

        if "instagram_profiles" in tables:
            try:
                row = await conn.fetchrow(
                    """
                    SELECT id, platform_user_id, username, followers_count, following_count,
                           posts_count, is_private, is_verified, updated_at, collected_at
                    FROM instagram_profiles
                    ORDER BY updated_at DESC NULLS LAST, collected_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    timeout=8,
                )
                report["latest_profile"] = _safe_row(row, [
                    "id", "platform_user_id", "username", "followers_count", "following_count",
                    "posts_count", "is_private", "is_verified", "updated_at", "collected_at",
                ])
            except Exception as exc:
                logger.warning("instagram health latest_profile lookup skipped: %s", exc.__class__.__name__)
                report["section_errors"]["latest_profile"] = exc.__class__.__name__

        if "instagram_posts" in tables:
            try:
                row = await conn.fetchrow(
                    """
                    WITH latest_post AS (
                        SELECT id, profile_id, platform_post_id, media_type,
                               likes_count, comments_count, platform_created_at, collected_at
                        FROM instagram_posts
                        ORDER BY collected_at DESC NULLS LAST
                        LIMIT 1
                    )
                    SELECT p.id, p.platform_post_id, ip.username, p.media_type,
                           p.likes_count, p.comments_count, p.platform_created_at, p.collected_at
                    FROM latest_post p
                    LEFT JOIN instagram_profiles ip ON ip.id = p.profile_id
                    """,
                    timeout=8,
                )
                report["latest_post"] = _safe_row(row, [
                    "id", "platform_post_id", "username", "media_type", "likes_count",
                    "comments_count", "platform_created_at", "collected_at",
                ])
            except Exception as exc:
                logger.warning("instagram health latest_post lookup skipped: %s", exc.__class__.__name__)
                report["section_errors"]["latest_post"] = exc.__class__.__name__

        if "media_items" in tables:
            try:
                row = await conn.fetchrow(
                    """
                    SELECT id, entity_name, content_type, content_id, file_size,
                           collected_at, source_url,
                           (COALESCE(metadata, '{}'::jsonb) ? 'vault_artifact') AS has_vault_artifact,
                           (COALESCE(metadata, '{}'::jsonb) ? 'vault_sidecar') AS has_vault_sidecar,
                           (COALESCE(metadata, '{}'::jsonb) ? 'raw_payload'
                            OR COALESCE(metadata, '{}'::jsonb) ? 'raw_payload_refs') AS has_raw_payload_ref,
                           metadata->'vault_artifact'->>'ok' AS vault_artifact_ok
                    FROM media_items
                    WHERE source = 'instagram'
                    ORDER BY collected_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    timeout=8,
                )
                report["latest_media"] = _safe_row(row, [
                    "id", "entity_name", "content_type", "content_id", "file_size",
                    "collected_at", "source_url", "has_vault_artifact", "has_vault_sidecar",
                    "has_raw_payload_ref", "vault_artifact_ok",
                ])
            except Exception as exc:
                logger.warning("instagram health latest_media lookup skipped: %s", exc.__class__.__name__)
                report["section_errors"]["latest_media"] = exc.__class__.__name__
                report["latest_media"] = None

        if "browser_ingest_events" in tables:
            row = await conn.fetchrow(
                """
                SELECT platform, endpoint, subject, observed_count, stored_count,
                       metadata->>'extension_version' AS extension_version,
                       metadata->>'message_type' AS message_type,
                       metadata->>'health_status' AS health_status,
                       metadata->>'health_reason' AS health_reason,
                       created_at
                FROM browser_ingest_events
                WHERE platform = 'instagram'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                timeout=3,
            )
            report["latest_browser_ingest"] = _safe_row(row, [
                "platform", "endpoint", "subject", "observed_count", "stored_count",
                "extension_version", "message_type", "health_status", "health_reason",
                "created_at",
            ])
            try:
                rows = await conn.fetch(
                    """
                    SELECT endpoint,
                           count(*)::int AS events,
                           COALESCE(sum(observed_count), 0)::int AS observed,
                           COALESCE(sum(stored_count), 0)::int AS stored,
                           max(created_at) AS latest_at
                    FROM browser_ingest_events
                    WHERE platform = 'instagram'
                      AND created_at >= NOW() - INTERVAL '24 hours'
                    GROUP BY endpoint
                    ORDER BY latest_at DESC
                    """,
                    timeout=5,
                )
                report["browser_ingest_24h"] = {
                    str(row["endpoint"]): {
                        "events": int(row["events"] or 0),
                        "observed": int(row["observed"] or 0),
                        "stored": int(row["stored"] or 0),
                        "latest_at": _iso_or_none(row["latest_at"]),
                    }
                    for row in rows
                }
            except Exception as exc:
                logger.warning("instagram health browser_ingest_24h lookup skipped: %s", exc.__class__.__name__)
                report["section_errors"]["browser_ingest_24h"] = exc.__class__.__name__

        if "browser_media_revisit_queue" in tables:
            rows = await conn.fetch(
                """
                SELECT COALESCE(status, 'unknown') AS status,
                       count(*)::int AS count,
                       count(*) FILTER (WHERE next_visit_at <= NOW())::int AS due,
                       count(*) FILTER (
                         WHERE status = 'claimed'
                           AND COALESCE(last_attempt_at, updated_at) < NOW() - INTERVAL '30 minutes'
                       )::int AS stale_claimed,
                       max(updated_at) AS latest_at
                FROM browser_media_revisit_queue
                WHERE platform = 'instagram'
                GROUP BY status
                ORDER BY status
                """,
                timeout=3,
            )
            by_status = {
                str(row["status"]): {
                    "count": int(row["count"] or 0),
                    "due": int(row["due"] or 0),
                    "stale_claimed": int(row["stale_claimed"] or 0),
                    "latest_at": _iso_or_none(row["latest_at"]),
                }
                for row in rows
            }
            report["revisit_queue"] = {
                "available": True,
                "by_status": by_status,
                "total": sum(item["count"] for item in by_status.values()),
                "due": sum(item["due"] for item in by_status.values()),
                "stale_claimed": sum(item["stale_claimed"] for item in by_status.values()),
            }

        if "realtime_media_deliveries" in tables:
            rows = await conn.fetch(
                """
                SELECT status, count(*)::int AS count
                FROM realtime_media_deliveries
                WHERE source = 'instagram'
                  AND updated_at >= NOW() - INTERVAL '24 hours'
                GROUP BY status
                ORDER BY status
                """,
                timeout=3,
            )
            latest = await conn.fetchrow(
                """
                SELECT content_id, status, reason, file_size, content_type,
                       target_name, queued_at, sent_at, updated_at
                FROM realtime_media_deliveries
                WHERE source = 'instagram'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                timeout=3,
            )
            report["realtime_delivery"] = {
                "available": True,
                "window_hours": 24,
                "status_counts": {str(row["status"]): int(row["count"] or 0) for row in rows},
                "latest": _safe_row(latest, [
                    "content_id", "status", "reason", "file_size", "content_type",
                    "target_name", "queued_at", "sent_at", "updated_at",
                ]),
            }

        if "rate_limit_events" in tables:
            has_cleared_by_success = bool(await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'rate_limit_events'
                      AND column_name = 'cleared_by_success'
                )
                """,
                timeout=3,
            ))
            cleared_predicate = "AND NOT COALESCE(cleared_by_success, false)" if has_cleared_by_success else ""
            active = await conn.fetchrow(
                f"""
                SELECT source, account, scope, status_code, reason, cooldown_seconds,
                       created_at,
                       created_at + COALESCE(cooldown_seconds, 0) * INTERVAL '1 second' AS active_until
                FROM rate_limit_events
                WHERE source = 'instagram'
                  AND COALESCE(cooldown_seconds, 0) > 0
                  AND created_at + COALESCE(cooldown_seconds, 0) * INTERVAL '1 second' > NOW()
                  {cleared_predicate}
                ORDER BY active_until DESC
                LIMIT 1
                """,
                timeout=3,
            )
            latest = await conn.fetchrow(
                """
                SELECT source, account, scope, status_code, reason, cooldown_seconds, created_at
                FROM rate_limit_events
                WHERE source = 'instagram'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                timeout=3,
            )
            report["cooldown"] = {
                "active": active is not None,
                "active_event": _safe_row(active, [
                    "source", "account", "scope", "status_code", "reason",
                    "cooldown_seconds", "created_at", "active_until",
                ]),
                "latest_event": _safe_row(latest, [
                    "source", "account", "scope", "status_code", "reason",
                    "cooldown_seconds", "created_at",
                ]),
            }

        if "source_health" in tables:
            row = await conn.fetchrow(
                """
                SELECT source, status, last_success_at, last_error, updated_at
                FROM source_health
                WHERE source = 'instagram'
                LIMIT 1
                """,
                timeout=3,
            )
            report["source_health"] = _safe_row(row, [
                "source", "status", "last_success_at", "last_error", "updated_at",
            ])

    fn = _lookup("_derive_instagram_stuck_stage") or _derive_instagram_stuck_stage
    report["stuck_stage"] = fn(report)
    return report


@router.get("/ingestion/hourly")
async def hourly_ingestion(hours: int = 12, _user: dict = Depends(require_role("viewer"))):
    """Hour-by-hour real ingestion from source tables, not collection_runs.

    collection_runs records scheduler trigger/rearm events. This endpoint reads
    the actual rows operators care about: posts/messages/activities/etc, media
    files, and rate-limit events.
    """
    hours = max(1, min(hours, 72))
    _INGESTION_HOURLY_CACHE = _lookup("_INGESTION_HOURLY_CACHE") or {}
    _INGESTION_CONTENT_PARTS = _lookup("_INGESTION_CONTENT_PARTS") or []

    def _fallback_rows(exc: BaseException) -> list[dict]:
        cached = _INGESTION_HOURLY_CACHE.get(hours)
        logger.warning("hourly ingestion failed: %s", exc.__class__.__name__)
        if cached and isinstance(cached.get("rows"), list):
            return [
                {**dict(row), "stats_stale": True, "stats_error": exc.__class__.__name__}
                for row in cached["rows"]  # type: ignore[index]
            ]
        return []

    pool = await _get_pool()
    content_parts = _INGESTION_CONTENT_PARTS
    required_tables = [table for _source, table, _column, _label in content_parts]
    required_tables.extend(["media_items", "rate_limit_events"])
    conn = None
    try:
        conn = await _acquire_dashboard_conn(pool)
        existing_tables = set(await conn.fetchval(
            """
            SELECT COALESCE(array_agg(table_name), ARRAY[]::text[])
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = ANY($1::text[])
            """,
            required_tables,
            timeout=8,
        ))
    except asyncio.CancelledError as exc:
        return _fallback_rows(exc)
    except Exception as exc:  # noqa: BLE001 - dashboard should degrade under DB load
        return _fallback_rows(exc)
    finally:
        if conn is not None:
            await _release_dashboard_conn(pool, conn, "hourly ingestion")
    raw_parts = [
        f"""
        SELECT '{source}'::text AS source,
               date_trunc('hour', {column}) AS hour,
               count(*)::bigint AS records,
               0::bigint AS media_items,
               {("count(*)" if label == "messages" else "0")}::bigint AS messages,
               0::bigint AS rate_limits,
               0::bigint AS access_errors
        FROM {table}
        WHERE {column} >= now() - ($1 || ' hours')::interval
        GROUP BY date_trunc('hour', {column})
        """
        for source, table, column, label in content_parts
        if table in existing_tables
    ]
    if "media_items" in existing_tables:
        raw_parts.append(
            """
            SELECT source,
                   date_trunc('hour', collected_at) AS hour,
                   0::bigint AS records,
                   count(*)::bigint AS media_items,
                   0::bigint AS messages,
                   0::bigint AS rate_limits,
                   0::bigint AS access_errors
            FROM media_items
            WHERE collected_at >= now() - ($1 || ' hours')::interval
            GROUP BY source, date_trunc('hour', collected_at)
            """
        )
    if "rate_limit_events" in existing_tables:
        raw_parts.append(
            """
            SELECT source,
                   date_trunc('hour', created_at) AS hour,
                   0::bigint AS records,
                   0::bigint AS media_items,
                   0::bigint AS messages,
                   count(*) FILTER (
                       WHERE status_code = 429
                          OR status_code IS NULL
                          OR cooldown_seconds IS NOT NULL
                          OR (
                              source = 'youtube'
                              AND status_code = 403
                              AND reason = 'youtube_api_quota_or_access'
                          )
                   )::bigint AS rate_limits,
                   count(*) FILTER (
                       WHERE status_code IS NOT NULL
                         AND NOT (
                             status_code = 429
                             OR cooldown_seconds IS NOT NULL
                             OR (
                                 source = 'youtube'
                                 AND status_code = 403
                                 AND reason = 'youtube_api_quota_or_access'
                             )
                         )
                   )::bigint AS access_errors
            FROM rate_limit_events
            WHERE created_at >= now() - ($1 || ' hours')::interval
            GROUP BY source, date_trunc('hour', created_at)
            """
        )
    if not raw_parts:
        return []
    sql = f"""
        WITH raw AS (
            {" UNION ALL ".join(raw_parts)}
        )
        SELECT source, hour,
               sum(records)::bigint AS records,
               sum(media_items)::bigint AS media_items,
               sum(messages)::bigint AS messages,
               sum(rate_limits)::bigint AS rate_limits,
               sum(access_errors)::bigint AS access_errors
        FROM raw
        GROUP BY source, hour
        ORDER BY hour DESC, source
    """
    labels = {source: label for source, _table, _column, label in content_parts}
    conn = None
    try:
        conn = await _acquire_dashboard_conn(pool)
        rows = await conn.fetch(sql, str(hours), timeout=30)
    except asyncio.CancelledError as exc:
        return _fallback_rows(exc)
    except Exception as exc:  # noqa: BLE001 - dashboard should degrade under DB load
        return _fallback_rows(exc)
    finally:
        if conn is not None:
            await _release_dashboard_conn(pool, conn, "rate limits recent")
    out = []
    for row in rows:
        d = dict(row)
        d["record_label"] = labels.get(d["source"], "records")
        out.append(d)
    _INGESTION_HOURLY_CACHE[hours] = {"ts": time.time(), "rows": [dict(row) for row in out]}
    return out
