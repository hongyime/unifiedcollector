"""Operations-facing observability routes for the dashboard.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 14
(cluster 8). Covers:

* ``/dlq`` — dead-letter-queue listing
* ``/domain-pacing/status`` — collector_domain_pacing_events summary
* ``/api-quotas/status`` — external API quota snapshots (github, youtube)

Cross-module helpers (``_jsonish``, ``_existing_public_tables``,
``_safe_estimated_table_rows``) are looked up on the parent
``src.dashboard.api`` module at call time so that test monkey-patches keep
working.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import asyncio

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
    """Read a name from the parent ``dashboard_api`` module at call time.

    Keeps test monkey-patch semantics: ``monkeypatch.setattr(dashboard_api,
    "_X", ...)`` continues to affect the extracted route because helpers are
    resolved via this lookup at request time.
    """
    root = sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


async def _get_pool():
    fn = _lookup("get_pool") or get_pool
    return await fn()


def _jsonish(value):
    """Local copy of the ``_jsonish`` helper.

    The parent module also exposes ``_jsonish``; use its version if present so
    a test patch on ``dashboard_api._jsonish`` still takes effect.
    """
    override = _lookup("_jsonish")
    if override is not None and override is not _jsonish:
        return override(value)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return value if isinstance(value, dict) else {}


router = APIRouter()


@router.get("/dlq")
async def list_dlq(source: str | None = None, limit: int = 50,
                   _user: dict = Depends(require_role("viewer"))):
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if source:
            rows = await conn.fetch(
                "SELECT * FROM dead_letter_queue WHERE source = $1 ORDER BY created_at DESC LIMIT $2",
                source, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM dead_letter_queue ORDER BY created_at DESC LIMIT $1",
                limit,
            )
    return [dict(r) for r in rows]


@router.get("/domain-pacing/status")
async def domain_pacing_status(source: str | None = None, hours: int = 24, limit: int = 50,
                               _user: dict = Depends(require_role("viewer"))):
    # Graceful degradation under DB load: asyncpg per-query timeouts must never
    # escape as HTTP 500 tracebacks.
    try:
        return await _domain_pacing_status_impl(source=source, hours=hours, limit=limit, _user=_user)
    except asyncio.TimeoutError:
        return {
            "available": False,
            "error": "timeout",
            "stats_unavailable": True,
            "sources": [],
            "domains": [],
            "events": [],
            "latest_snapshots": {},
        }


async def _domain_pacing_status_impl(source: str | None = None, hours: int = 24, limit: int = 50,
                                     _user: dict = Depends(require_role("viewer"))):
    hours = max(1, min(hours, 168))
    limit = max(1, min(limit, 250))
    source_filter = (source or "").strip().lower() or None
    if source_filter and source_filter not in {"website", "search"}:
        return {"available": False, "error": "unsupported_source", "sources": []}

    pool = await _get_pool()
    conn = None
    try:
        conn = await _acquire_dashboard_conn(pool)
        table_exists = bool(await conn.fetchval(
            "SELECT to_regclass('public.collector_domain_pacing_events') IS NOT NULL",
            timeout=8,
        ))
        if not table_exists:
            return {
                "available": False,
                "error": "collector_domain_pacing_events_missing",
                "sources": [],
                "domains": [],
                "events": [],
            }

        summary_args: list[object] = [str(hours)]
        summary_source_sql = ""
        if source_filter:
            summary_args.append(source_filter)
            summary_source_sql = f"AND source = ${len(summary_args)}"
        summary_rows = [dict(r) for r in await conn.fetch(
            f"""
            SELECT source,
                   COUNT(DISTINCT registrable_domain)::int AS domains_seen,
                   COUNT(DISTINCT registrable_domain) FILTER (
                       WHERE created_at >= now() - interval '15 minutes'
                   )::int AS recently_active_domains,
                   COUNT(*) FILTER (WHERE event_type = 'robots_blocked')::int AS robots_blocked,
                   COUNT(*) FILTER (WHERE event_type = 'retry_backoff')::int AS retry_backoff,
                   COUNT(*) FILTER (WHERE status_code = 403)::int AS http_403,
                   COUNT(*) FILTER (WHERE status_code = 429)::int AS http_429,
                   COALESCE(SUM((metadata->>'images')::int) FILTER (WHERE event_type = 'crawl_summary'), 0)::int AS media_found,
                   COALESCE(SUM((metadata->>'pdfs')::int) FILTER (WHERE event_type = 'crawl_summary'), 0)::int AS pdfs_found,
                   COALESCE(SUM((metadata->>'docs')::int) FILTER (WHERE event_type = 'crawl_summary'), 0)::int AS docs_found,
                   COALESCE(SUM((metadata->>'videos')::int) FILTER (WHERE event_type = 'crawl_summary'), 0)::int AS videos_found,
                   MAX(created_at) AS latest_event_at
            FROM collector_domain_pacing_events
            WHERE created_at >= now() - ($1 || ' hours')::interval
              {summary_source_sql}
            GROUP BY source
            ORDER BY source
            """,
            *summary_args,
            timeout=15,
        )]
        paged_args: list[object] = [str(hours), limit]
        paged_source_sql = ""
        if source_filter:
            paged_args.append(source_filter)
            paged_source_sql = f"AND source = ${len(paged_args)}"
        domain_rows = [dict(r) for r in await conn.fetch(
            f"""
            SELECT source, registrable_domain,
                   COUNT(*)::int AS events,
                   COUNT(*) FILTER (WHERE event_type = 'robots_blocked')::int AS robots_blocked,
                   COUNT(*) FILTER (WHERE event_type = 'retry_backoff')::int AS retry_backoff,
                   COUNT(*) FILTER (WHERE status_code = 403)::int AS http_403,
                   COUNT(*) FILTER (WHERE status_code = 429)::int AS http_429,
                   COALESCE(SUM((metadata->>'images')::int) FILTER (WHERE event_type = 'crawl_summary'), 0)::int AS media_found,
                   COALESCE(SUM((metadata->>'pdfs')::int) FILTER (WHERE event_type = 'crawl_summary'), 0)::int AS pdfs_found,
                   COALESCE(SUM((metadata->>'docs')::int) FILTER (WHERE event_type = 'crawl_summary'), 0)::int AS docs_found,
                   COALESCE(SUM((metadata->>'videos')::int) FILTER (WHERE event_type = 'crawl_summary'), 0)::int AS videos_found,
                   MAX(created_at) AS latest_event_at
            FROM collector_domain_pacing_events
            WHERE created_at >= now() - ($1 || ' hours')::interval
              {paged_source_sql}
            GROUP BY source, registrable_domain
            ORDER BY latest_event_at DESC
            LIMIT $2
            """,
            *paged_args,
            timeout=15,
        )]
        event_rows = [dict(r) for r in await conn.fetch(
            f"""
            SELECT source, registrable_domain, host, event_type, url, status_code,
                   metadata, created_at
            FROM collector_domain_pacing_events
            WHERE created_at >= now() - ($1 || ' hours')::interval
              {paged_source_sql}
            ORDER BY created_at DESC
            LIMIT $2
            """,
            *paged_args,
            timeout=15,
        )]
        snapshot_source_sql = "AND source = $1" if source_filter else ""
        snapshot_args: list[object] = [source_filter] if source_filter else []
        snapshot_rows = [dict(r) for r in await conn.fetch(
            f"""
            SELECT DISTINCT ON (source) source, metadata, created_at
            FROM collector_domain_pacing_events
            WHERE event_type = 'crawl_summary'
              {snapshot_source_sql}
            ORDER BY source, created_at DESC
            """,
            *snapshot_args,
            timeout=15,
        )]
    finally:
        if conn is not None:
            await _release_dashboard_conn(pool, conn, "domain pacing")

    latest_snapshots = {}
    for row in snapshot_rows:
        meta = _jsonish(row.get("metadata"))
        pacing = _jsonish(meta.get("domain_pacing"))
        latest_snapshots[row["source"]] = {
            "created_at": _iso_or_none(row.get("created_at")),
            "active_domains": int(pacing.get("active_domains") or 0),
            "per_domain_inflight": pacing.get("per_domain_inflight") or {},
            "max_active_domains": pacing.get("max_active_domains"),
            "max_per_domain": pacing.get("max_per_domain"),
            "delay_seconds": pacing.get("delay_seconds"),
            "jitter_seconds": pacing.get("jitter_seconds"),
            "counters": pacing.get("counters") or {},
        }
    for rows in (summary_rows, domain_rows, event_rows):
        for row in rows:
            if "created_at" in row:
                row["created_at"] = _iso_or_none(row["created_at"])
            if "latest_event_at" in row:
                row["latest_event_at"] = _iso_or_none(row["latest_event_at"])
            if "metadata" in row:
                row["metadata"] = _jsonish(row["metadata"])
    return {
        "available": True,
        "hours": hours,
        "source": source_filter,
        "sources": summary_rows,
        "domains": domain_rows,
        "events": event_rows,
        "latest_snapshots": latest_snapshots,
    }


@router.get("/api-quotas/status")
async def api_quotas_status(service: str | None = None, limit: int = 100,
                            _user: dict = Depends(require_role("viewer"))):
    limit = max(1, min(limit, 250))
    service_filter = (service or "").strip().lower() or None
    if service_filter and service_filter not in {"github", "youtube"}:
        return {"available": False, "error": "unsupported_service", "snapshots": []}

    pool = await _get_pool()
    conn = None
    try:
        conn = await _acquire_dashboard_conn(pool)
        table_names = [
            "collector_api_quota_snapshots",
            "account_quota_usage",
            "github_spider_queue",
            "github_users",
            "github_repos",
            "github_commits",
            "github_issues",
            "github_issue_comments",
            "github_pr_reviews",
            "github_pr_review_comments",
            "github_edges",
            "media_items",
            "collector_operational_events",
            "youtube_spider_queue",
            "youtube_profile_queue",
            "youtube_videos",
        ]
        _existing_public_tables = _lookup("_existing_public_tables")
        _safe_estimated_table_rows = _lookup("_safe_estimated_table_rows")
        tables = await _existing_public_tables(conn, table_names)
        snapshots = []
        if "collector_api_quota_snapshots" in tables:
            service_sql = "WHERE service = $2" if service_filter else ""
            snapshot_args: list[object] = [limit]
            if service_filter:
                snapshot_args.append(service_filter)
            snapshots = [dict(r) for r in await conn.fetch(
                f"""
                SELECT service, account, bucket, quota_date, reset_at,
                       used_units, remaining_units, quota_units, target_units,
                       target_ratio, paused, metadata, updated_at
                FROM collector_api_quota_snapshots
                {service_sql}
                ORDER BY updated_at DESC
                LIMIT $1
                """,
                *snapshot_args,
                timeout=15,
            )]
        quota_usage = []
        if "account_quota_usage" in tables:
            service_sql = "AND platform = $2" if service_filter else ""
            usage_args: list[object] = [limit]
            if service_filter:
                usage_args.append(service_filter)
            quota_usage = [dict(r) for r in await conn.fetch(
                f"""
                SELECT platform AS service, account, day, requests_today,
                       requests_hour, hour_bucket, requests_week, updated_at
                FROM account_quota_usage
                WHERE platform IN ('github', 'youtube')
                  {service_sql}
                ORDER BY updated_at DESC
                LIMIT $1
                """,
                *usage_args,
                timeout=15,
            )]

        async def _table_count(table: str, where: str = "") -> int:
            if table not in tables:
                return 0
            try:
                if not where:
                    return await _safe_estimated_table_rows(conn, table)
                return int(await conn.fetchval(f"SELECT COUNT(*)::bigint FROM {table} {where}", timeout=3) or 0)
            except Exception:
                return 0

        async def _status_counts(table: str, column: str = "status") -> dict[str, int]:
            if table not in tables:
                return {}
            try:
                rows = await conn.fetch(
                    f"SELECT COALESCE({column}::text, 'unknown') AS status, COUNT(*)::bigint AS count FROM {table} GROUP BY 1",
                    timeout=3,
                )
                return {str(r["status"]): int(r["count"] or 0) for r in rows}
            except Exception:
                return {}

        async def _github_recent_transport_blocked() -> bool:
            if "collector_operational_events" not in tables:
                return False
            try:
                return bool(await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM collector_operational_events
                        WHERE source = 'github'
                          AND event_type = 'api_transport_exhausted'
                          AND created_at >= now() - interval '15 minutes'
                    )
                    """,
                    timeout=3,
                ))
            except Exception:
                return False

        def _github_quota_pusher_status(spider_counts: dict[str, int], transport_blocked: bool) -> dict:
            enabled = os.getenv("GITHUB_QUOTA_PUSHER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
            try:
                target_ratio = min(0.95, max(0.5, float(os.getenv("GITHUB_QUOTA_PUSHER_TARGET_RATIO", "0.80"))))
            except (TypeError, ValueError):
                target_ratio = 0.80
            try:
                max_concurrent = min(12, max(1, int(os.getenv("GITHUB_QUOTA_PUSHER_MAX_CONCURRENT", "12"))))
            except (TypeError, ValueError):
                max_concurrent = 12
            try:
                batch_size = max(1, int(os.getenv("GITHUB_QUOTA_PUSHER_BATCH_SIZE", "250")))
            except (TypeError, ValueError):
                batch_size = 250
            pending = int(spider_counts.get("pending") or 0)
            reason = "disabled"
            github_snapshots = [
                row for row in snapshots
                if row.get("service") == "github" and row.get("bucket") == "core_hour"
            ]
            if enabled:
                if pending <= 0:
                    reason = "idle_no_work"
                elif transport_blocked:
                    reason = "transport_blocked"
                elif not github_snapshots:
                    reason = "quota_fill_active"
                else:
                    below_target = False
                    for row in github_snapshots:
                        quota_units = int(row.get("quota_units") or 0)
                        used_units = int(row.get("used_units") or 0)
                        paused = bool(row.get("paused"))
                        target_units_value = (
                            max(1, int(quota_units * target_ratio))
                            if quota_units > 0
                            else int(row.get("target_units") or 0)
                        )
                        if quota_units > 0 and not paused and used_units < target_units_value:
                            below_target = True
                            break
                    reason = "quota_fill_active" if below_target else "target_reached"
            return {
                "enabled": enabled,
                "target_ratio": target_ratio,
                "max_concurrent": max_concurrent,
                "batch_size": batch_size,
                "pending": pending,
                "transport_blocked": transport_blocked,
                "reason": reason,
            }

        progress = {}
        if service_filter in {None, "github"}:
            github_spider_counts = await _status_counts("github_spider_queue")
            github_transport_blocked = await _github_recent_transport_blocked()
            progress["github"] = {
                "capabilities": {
                    "sequential_profile_order": True,
                    "branch_commits_enabled": True,
                    "commits_enabled": True,
                    "issues_enabled": True,
                    "pr_reviews_enabled": True,
                    "pr_review_comments_enabled": True,
                    "contributors_enabled": True,
                    "followers_following_queue_enabled": os.getenv("GITHUB_SPIDER_ENABLED", "true").lower() == "true",
                    "releases_assets_enabled": True,
                },
                "queues": {
                    "spider": github_spider_counts,
                },
                "quota_pusher": _github_quota_pusher_status(
                    github_spider_counts,
                    github_transport_blocked,
                ),
                "tables": {
                    "users": await _table_count("github_users"),
                    "repos": await _table_count("github_repos"),
                    "commits": await _table_count("github_commits"),
                    "issues": await _table_count("github_issues"),
                    "pull_requests": await _table_count("github_issues", "WHERE is_pull_request = true"),
                    "issue_comments": await _table_count("github_issue_comments"),
                    "pr_reviews": await _table_count("github_pr_reviews"),
                    "pr_review_comments": await _table_count("github_pr_review_comments"),
                    "contributor_edges": await _table_count("github_edges", "WHERE edge_type = 'repo_contributor'"),
                    "release_assets": await _table_count(
                        "media_items",
                        "WHERE source = 'github' AND content_type IN ('release', 'release_asset')",
                    ),
                },
            }
        if service_filter in {None, "youtube"}:
            progress["youtube"] = {
                "capabilities": {
                    "daily_quota_budget_controller": True,
                    "midnight_pt_reset": True,
                    "search_bucket_units": int(os.getenv("YOUTUBE_SEARCH_DAILY_QUOTA_UNITS", "1000")),
                    "data_api_low_cost_first": True,
                    "search_list_guarded": True,
                },
                "queues": {
                    "spider": await _status_counts("youtube_spider_queue"),
                    "profile": await _status_counts("youtube_profile_queue"),
                },
                "videos": {
                    "media_status": await _status_counts("youtube_videos", "media_status"),
                    "transcript_status": await _status_counts("youtube_videos", "transcript_status"),
                    "comments_status": await _status_counts("youtube_videos", "comments_status"),
                    "total": await _table_count("youtube_videos"),
                },
            }
    finally:
        if conn is not None:
            await _release_dashboard_conn(pool, conn, "api quotas")

    for rows in (snapshots, quota_usage):
        for row in rows:
            for key in ("quota_date", "day", "reset_at", "updated_at"):
                if key in row:
                    row[key] = _iso_or_none(row[key])
            if "metadata" in row:
                row["metadata"] = _jsonish(row["metadata"])
    return {
        "available": bool(snapshots or quota_usage or progress),
        "service": service_filter,
        "snapshots": snapshots,
        "account_quota_usage": quota_usage,
        "progress": progress,
    }
