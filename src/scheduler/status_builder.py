"""Status/heartbeat snapshot builders — pure reporting utilities.

Extracted from ``src/scheduler/__init__.py`` in the LOGIC-005 refactor
(``docs/plans/scheduler-refactor.md``). None of this is scheduler logic; it is
DB-fanout reporting that assembles a snapshot for the Telegram heartbeat and
the 15-minute delta notification.

The functions here are module-level and accept ``pool`` / ``conn`` /
``freshness`` explicitly so they are unit-testable without instantiating
``Scheduler``. The ``Scheduler`` class keeps instance-method wrappers
(``_build_status`` / ``_build_status_delta`` / ``_delta_*``) that delegate here
— existing callers and tests continue to work unchanged.

Helpers previously in ``scheduler/__init__.py`` at module scope
(``_expected_extension_version``, ``_browser_maintenance_status``,
``_tiktok_revisit_claim_timeout_seconds``, ``_STATUS_CONTENT_PARTS``,
``_summarize_ingestion_window``, ``_fetch_ingestion_window``) moved here
too — they exist only to serve status assembly.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.backup.db_backup import backup_status
from src.core.env import env_int
from src.core.vault import VAULT_ROOT, vault_artifact_counts, vault_health

logger = logging.getLogger(__name__)


# --- Small module-level helpers ---------------------------------------------


def _expected_extension_version() -> str | None:
    """Best-effort repo extension version for operator status messages."""
    env_version = str(os.getenv("UC_EXTENSION_EXPECTED_VERSION") or "").strip()
    if env_version:
        return env_version
    try:
        manifest = Path(__file__).resolve().parents[2] / "extension" / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        version = str(data.get("version") or "").strip()
        return version or None
    except Exception:
        return None


def _browser_maintenance_status() -> dict | None:
    path = Path(os.getenv("BROWSER_TAB_MAINTENANCE_STATUS_PATH", "/app/tmp/browser_tab_maintenance_status.json"))
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return None
        try:
            checked_at = datetime.fromisoformat(str(data.get("checked_at") or ""))
            now = datetime.now(checked_at.tzinfo) if checked_at.tzinfo else datetime.now()
            data["age_seconds"] = max(0, int((now - checked_at).total_seconds()))
        except Exception:
            data["age_seconds"] = None
        return data
    except Exception:
        return None


def _tiktok_revisit_claim_timeout_seconds() -> int:
    try:
        return env_int("TIKTOK_BROWSER_REVISIT_CLAIM_TIMEOUT_SECONDS", 1800, min_value=60)
    except Exception:
        return 1800


_STATUS_CONTENT_PARTS = (
    ("telegram", "telegram_messages", "collected_at", "messages"),
    ("whatsapp", "whatsapp_messages", "collected_at", "messages"),
    ("beeper", "beeper_shadow_messages", "ingested_at", "messages"),
    ("instagram", "instagram_posts", "collected_at", "posts"),
    ("tiktok", "tiktok_posts", "collected_at", "posts"),
    ("lemon8", "lemon8_posts", "collected_at", "posts"),
    ("threads", "threads_posts", "collected_at", "posts"),
    ("facebook", "facebook_posts", "collected_at", "posts"),
    ("x", "x_posts", "collected_at", "posts"),
    ("youtube", "youtube_videos", "collected_at", "videos"),
    ("github", "github_commits", "collected_at", "commits"),
    ("website", "website_pages", "collected_at", "pages"),
    ("strava", "strava_activities", "collected_at", "activities"),
    ("search", "search_results", "collected_at", "results"),
)


def _summarize_ingestion_window(by_source: dict[str, dict[str, int]]) -> dict:
    totals = {
        "records": sum(v.get("records", 0) for v in by_source.values()),
        "messages": sum(v.get("messages", 0) for v in by_source.values()),
        "files": sum(v.get("files", 0) for v in by_source.values()),
        "rate_limits": sum(v.get("rate_limits", 0) for v in by_source.values()),
        "access_errors": sum(v.get("access_errors", 0) for v in by_source.values()),
    }
    top = sorted(
        ({"source": k, **v} for k, v in by_source.items()),
        key=lambda r: (
            r.get("records", 0) + r.get("files", 0) + r.get("rate_limits", 0) + r.get("access_errors", 0),
            r["source"],
        ),
        reverse=True,
    )[:6]
    return {"totals": totals, "sources": top}


async def _fetch_ingestion_window(conn, start_sql: str, end_sql: str | None = None) -> dict:
    by_source: dict[str, dict[str, int]] = {}
    for src, tbl, col, label in _STATUS_CONTENT_PARTS:
        end_clause = f" AND {col} < {end_sql}" if end_sql else ""
        try:
            n = int(await conn.fetchval(
                f"SELECT count(*) FROM {tbl} WHERE {col} >= {start_sql}{end_clause}",
                timeout=10,
            ) or 0)
        except Exception:
            continue
        if n:
            by_source[src] = {
                "records": n,
                "messages": n if label == "messages" else 0,
                "files": 0,
                "rate_limits": 0,
                "access_errors": 0,
            }
    media_end_clause = f" AND collected_at < {end_sql}" if end_sql else ""
    try:
        for row in await conn.fetch(
            f"""
            SELECT source, count(*)::int AS files
            FROM media_items
            WHERE collected_at >= {start_sql}
              {media_end_clause}
            GROUP BY source
            """,
            timeout=15,
        ):
            d = by_source.setdefault(row["source"], {
                "records": 0,
                "messages": 0,
                "files": 0,
                "rate_limits": 0,
                "access_errors": 0,
            })
            d["files"] = int(row["files"] or 0)
    except Exception:
        pass
    rate_end_clause = f" AND created_at < {end_sql}" if end_sql else ""
    try:
        for row in await conn.fetch(
            f"""
            SELECT source,
                   count(*) FILTER (
                     WHERE status_code = 429
                        OR status_code IS NULL
                        OR (
                            source = 'youtube'
                            AND status_code = 403
                            AND reason = 'youtube_api_quota_or_access'
                        )
                   )::int AS rate_limits,
                   count(*) FILTER (
                     WHERE status_code IS NOT NULL
                       AND NOT (
                           status_code = 429
                           OR (
                               source = 'youtube'
                               AND status_code = 403
                               AND reason = 'youtube_api_quota_or_access'
                           )
                       )
                   )::int AS access_errors
            FROM rate_limit_events
            WHERE created_at >= {start_sql}
              {rate_end_clause}
            GROUP BY source
            """,
            timeout=10,
        ):
            d = by_source.setdefault(row["source"], {
                "records": 0,
                "messages": 0,
                "files": 0,
                "rate_limits": 0,
                "access_errors": 0,
            })
            d["rate_limits"] = int(row["rate_limits"] or 0)
            d["access_errors"] = int(row["access_errors"] or 0)
    except Exception:
        pass
    return _summarize_ingestion_window(by_source)


# --- Public snapshot builders ------------------------------------------------


async def build_status(pool, freshness: list[tuple[str, str, int]]) -> dict:
    """Accurate collector-health snapshot for the heartbeat.

    ``freshness`` is the ``(source, query, threshold_seconds)`` table the
    scheduler considers canonical (see ``src.core.source_freshness``); it is
    passed in so this module doesn't reach back into ``Scheduler``.

    Every DB call is independently guarded so a missing table / slow query
    degrades gracefully — the snapshot is best-effort and returns whatever it
    could assemble.
    """
    snap: dict = {"ok": True}
    try:
        async with pool.acquire() as conn:
            # Operator heartbeats must be quick. Use the planner estimate for
            # all-time media total; exact 24h/hourly counts below stay real.
            try:
                snap["media_items"] = int(await conn.fetchval(
                    "SELECT reltuples::bigint FROM pg_class WHERE relname='media_items'",
                    timeout=5) or 0)
                snap["media_items_estimate"] = True
            except Exception:
                pass

            # Real 24h media ingestion (uses idx_media_collected — fast).
            try:
                snap["media_24h"] = int(await conn.fetchval(
                    "SELECT count(*) FROM media_items WHERE collected_at > now()-interval '24 hours'",
                    timeout=30) or 0)
            except Exception:
                pass

            # Real 24h messages across all 3 realtime platforms.
            try:
                snap["msgs_24h"] = int(await conn.fetchval(
                    """
                    SELECT (SELECT count(*) FROM telegram_messages     WHERE collected_at > now()-interval '24 hours')
                         + (SELECT count(*) FROM whatsapp_messages      WHERE collected_at > now()-interval '24 hours')
                         + (SELECT count(*) FROM beeper_shadow_messages WHERE ingested_at  > now()-interval '24 hours')
                    """, timeout=30) or 0)
            except Exception:
                pass

            # Current clock-hour ingestion for the Telegram heartbeat. This
            # mirrors the dashboard's early-warning view but keeps the
            # heartbeat payload compact.
            try:
                current = await _fetch_ingestion_window(conn, "date_trunc('hour', now())")
                previous = await _fetch_ingestion_window(
                    conn,
                    "date_trunc('hour', now()) - interval '1 hour'",
                    "date_trunc('hour', now())",
                )
                snap["hourly_ingestion"] = {
                    **current,
                    "previous_complete_hour": previous,
                }
            except Exception:
                pass

            try:
                snap["rate_limit_events"] = [dict(r) for r in await conn.fetch(
                    """
                    SELECT source, account, scope,
                           max(status_code)::int AS status_code,
                           count(*)::int AS count,
                           max(cooldown_seconds)::int AS cooldown_seconds,
                           max(reason) AS reason,
                           max(created_at) AS last_seen_at
                    FROM rate_limit_events
                    WHERE created_at >= date_trunc('hour', now())
                      AND (
                          status_code = 429
                          OR status_code IS NULL
                          OR (
                              source = 'youtube'
                              AND status_code = 403
                              AND reason = 'youtube_api_quota_or_access'
                          )
                      )
                    GROUP BY source, account, scope
                    ORDER BY last_seen_at DESC
                    LIMIT 5
                    """,
                    timeout=10,
                )]
            except Exception:
                pass

            try:
                snap["access_events"] = [dict(r) for r in await conn.fetch(
                    """
                    SELECT source, account, scope,
                           max(status_code)::int AS status_code,
                           count(*)::int AS count,
                           max(reason) AS reason,
                           max(created_at) AS last_seen_at
                    FROM rate_limit_events
                    WHERE created_at >= date_trunc('hour', now())
                      AND status_code IS NOT NULL
                      AND NOT (
                          status_code = 429
                          OR (
                              source = 'youtube'
                              AND status_code = 403
                              AND reason = 'youtube_api_quota_or_access'
                          )
                      )
                    GROUP BY source, account, scope
                    ORDER BY last_seen_at DESC
                    LIMIT 5
                    """,
                    timeout=10,
                )]
            except Exception:
                pass

            try:
                if await conn.fetchval("SELECT to_regclass('account_quota_usage')", timeout=5) is not None:
                    youtube_daily_limit = int(os.getenv("YOUTUBE_API_DAILY_QUOTA", "10000") or "10000")
                    snap["quota_usage"] = [dict(r) for r in await conn.fetch(
                        """
                        SELECT platform,
                               account,
                               requests_hour::bigint AS requests_hour,
                               requests_today::bigint AS requests_today,
                               requests_week::bigint AS requests_week,
                               hour_bucket,
                               updated_at,
                               CASE
                                 WHEN platform = 'github' THEN 5000
                                 ELSE NULL
                               END::int AS hourly_limit,
                               CASE
                                 WHEN platform = 'youtube' THEN $1::int
                                 ELSE NULL
                               END::int AS daily_limit
                        FROM account_quota_usage
                        WHERE day >= (NOW() AT TIME ZONE 'Asia/Singapore')::date
                        ORDER BY
                            CASE WHEN platform = 'github' THEN 0 ELSE 1 END,
                            requests_hour DESC,
                            updated_at DESC
                        LIMIT 10
                        """,
                        youtube_daily_limit,
                        timeout=10,
                    )]
            except Exception:
                pass

            try:
                if await conn.fetchval("SELECT to_regclass('dm_hook_heartbeat')", timeout=5) is not None:
                    expected_ext_version = _expected_extension_version()
                    probe_counts: dict[str, dict[str, int]] = {}
                    if await conn.fetchval("SELECT to_regclass('dm_probe_log')", timeout=5) is not None:
                        for row in await conn.fetch(
                            """
                            SELECT platform,
                                   count(*) FILTER (
                                     WHERE event_type = 'probe'
                                       AND seen_at >= date_trunc('hour', now())
                                   )::int AS probes_current_hour,
                                   count(*) FILTER (
                                     WHERE event_type = 'sample'
                                       AND seen_at >= date_trunc('hour', now())
                                   )::int AS samples_current_hour,
                                   max(seen_at) AS last_frame_seen_at
                            FROM dm_probe_log
                            GROUP BY platform
                            """,
                            timeout=10,
                        ):
                            probe_counts[str(row["platform"])] = {
                                "probes_current_hour": int(row["probes_current_hour"] or 0),
                                "samples_current_hour": int(row["samples_current_hour"] or 0),
                                "last_frame_age_seconds": int(
                                    (
                                        datetime.now(timezone.utc) - row["last_frame_seen_at"]
                                    ).total_seconds()
                                ) if row["last_frame_seen_at"] else None,
                            }
                    hooks = []
                    for row in await conn.fetch(
                        """
                        SELECT platform,
                               max(last_seen) AS last_seen_at,
                               extract(epoch FROM now() - max(last_seen))::int AS age_seconds,
                               sum(probes_sent)::int AS probes_sent,
                               sum(samples_shipped)::int AS samples_shipped,
                               (array_agg(extension_version ORDER BY last_seen DESC))[1] AS extension_version,
                               count(*) FILTER (WHERE COALESCE(owner_account, '') <> '')::int AS owner_count
                        FROM dm_hook_heartbeat
                        GROUP BY platform
                        ORDER BY last_seen_at DESC
                        """,
                        timeout=10,
                    ):
                        platform = str(row["platform"])
                        counts = probe_counts.get(platform, {})
                        hooks.append({
                            "platform": platform,
                            "age_seconds": int(row["age_seconds"] or 0),
                            "probes_sent": int(row["probes_sent"] or 0),
                            "samples_shipped": int(row["samples_shipped"] or 0),
                            "extension_version": row["extension_version"],
                            "expected_extension_version": expected_ext_version,
                            "owner_count": int(row["owner_count"] or 0),
                            **counts,
                        })
                    snap["extension_hooks"] = hooks
            except Exception:
                pass

            try:
                if await conn.fetchval("SELECT to_regclass('browser_ingest_events')", timeout=5) is not None:
                    snap["browser_ingest_events"] = [dict(r) for r in await conn.fetch(
                        """
                        SELECT platform,
                               endpoint,
                               count(*)::int AS requests,
                               sum(observed_count)::int AS observed_count,
                               sum(stored_count)::int AS stored_count,
                               max(created_at) AS last_seen_at
                        FROM browser_ingest_events
                        WHERE created_at >= date_trunc('hour', now())
                        GROUP BY platform, endpoint
                        ORDER BY observed_count DESC, stored_count DESC, last_seen_at DESC
                        LIMIT 8
                        """,
                        timeout=10,
                    )]
                    health_rows = [dict(r) for r in await conn.fetch(
                        """
                        SELECT platform,
                               endpoint,
                               count(*)::int AS requests,
                               sum(observed_count)::int AS observed_count,
                               sum(stored_count)::int AS stored_count,
                               max(created_at) AS last_seen_at,
                               extract(epoch FROM now() - max(created_at))::int AS age_seconds
                        FROM browser_ingest_events
                        WHERE created_at >= now() - interval '30 minutes'
                        GROUP BY platform, endpoint
                        ORDER BY last_seen_at DESC
                        LIMIT 20
                        """,
                        timeout=10,
                    )]
                    fresh_after = env_int("BROWSER_INGEST_ACTIVE_SECONDS", 600, min_value=60)
                    fresh = [r for r in health_rows if int(r.get("age_seconds") or 0) <= fresh_after]
                    fresh_content = [
                        r for r in fresh
                        if str(r.get("endpoint") or "") != "browser_heartbeat"
                        and (int(r.get("observed_count") or 0) > 0 or int(r.get("stored_count") or 0) > 0)
                    ]
                    snap["browser_ingest_health"] = {
                        "state": "active" if fresh else ("stale" if health_rows else "missing"),
                        "active": bool(fresh),
                        "heartbeat_active": any(str(r.get("endpoint") or "") == "browser_heartbeat" for r in fresh),
                        "content_active": bool(fresh_content),
                        "active_platforms": sorted({str(r.get("platform")) for r in fresh if r.get("platform")}),
                        "content_platforms": sorted({str(r.get("platform")) for r in fresh_content if r.get("platform")}),
                        "last_seen_age_seconds": min((int(r.get("age_seconds") or 0) for r in health_rows), default=None),
                        "last_content_age_seconds": min(
                            (
                                int(r.get("age_seconds") or 0)
                                for r in health_rows
                                if str(r.get("endpoint") or "") != "browser_heartbeat"
                                and (int(r.get("observed_count") or 0) > 0 or int(r.get("stored_count") or 0) > 0)
                            ),
                            default=None,
                        ),
                        "fresh_after_seconds": fresh_after,
                    }
                    maintenance = _browser_maintenance_status()
                    if maintenance:
                        snap["browser_maintenance"] = maintenance
                    content_stale_seconds = env_int(
                        "BROWSER_CONTENT_STALE_WARN_SECONDS",
                        3600,
                        min_value=300,
                    )
                    snap["browser_content_gaps"] = [dict(r) for r in await conn.fetch(
                        """
                        WITH heartbeat AS (
                            SELECT DISTINCT ON (platform)
                                   platform,
                                   created_at AS heartbeat_at,
                                   metadata
                            FROM browser_ingest_events
                            WHERE endpoint = 'browser_heartbeat'
                              AND platform = ANY($2::text[])
                            ORDER BY platform, created_at DESC
                        ),
                        content AS (
                            SELECT DISTINCT ON (platform)
                                   platform,
                                   created_at AS last_content_at
                            FROM browser_ingest_events
                            WHERE endpoint <> 'browser_heartbeat'
                              AND (observed_count > 0 OR stored_count > 0)
                              AND platform = ANY($2::text[])
                            ORDER BY platform, created_at DESC
                        )
                        SELECT heartbeat.platform,
                               heartbeat.heartbeat_at,
                               extract(epoch FROM now() - heartbeat.heartbeat_at)::int AS heartbeat_age_seconds,
                               content.last_content_at,
                               extract(epoch FROM now() - content.last_content_at)::int AS content_age_seconds,
                               heartbeat.metadata->>'url' AS url,
                               heartbeat.metadata->>'health_status' AS health_status,
                               heartbeat.metadata->>'extension_version' AS extension_version,
                               heartbeat.metadata->'content_counts' AS content_counts,
                               $1::int AS stale_after_seconds
                        FROM heartbeat
                        LEFT JOIN content ON content.platform = heartbeat.platform
                        WHERE heartbeat.heartbeat_at < now() - ($1::int * interval '1 second')
                           OR content.last_content_at IS NULL
                           OR content.last_content_at < now() - ($1::int * interval '1 second')
                        ORDER BY
                          CASE
                            WHEN heartbeat.heartbeat_at < now() - ($1::int * interval '1 second') THEN 0
                            ELSE 1
                          END,
                          heartbeat.heartbeat_at DESC
                        LIMIT 8
                        """,
                        content_stale_seconds,
                        ["instagram", "tiktok", "lemon8", "threads", "facebook", "x", "strava"],
                        timeout=10,
                    )]
            except Exception:
                pass

            try:
                if await conn.fetchval("SELECT to_regclass('browser_media_candidates')", timeout=5) is not None:
                    snap["browser_media_diagnostics"] = [dict(r) for r in await conn.fetch(
                        """
                        SELECT platform,
                               outcome,
                               count(*)::int AS candidates,
                               count(*) FILTER (WHERE needs_revisit)::int AS needs_revisit,
                               max(last_seen) AS last_seen_at
                        FROM browser_media_candidates
                        WHERE last_seen >= date_trunc('hour', now())
                        GROUP BY platform, outcome
                        ORDER BY platform, candidates DESC, last_seen_at DESC
                        LIMIT 30
                        """,
                        timeout=10,
                    )]
                if await conn.fetchval("SELECT to_regclass('browser_media_revisit_queue')", timeout=5) is not None:
                    claim_timeout = _tiktok_revisit_claim_timeout_seconds()
                    snap["browser_media_revisit_queue"] = [dict(r) for r in await conn.fetch(
                        """
                        SELECT platform,
                               count(*) FILTER (
                                 WHERE status IN ('pending', 'failed')
                                   AND next_visit_at <= now()
                               )::int AS due,
                               count(*) FILTER (WHERE status = 'claimed')::int AS claimed,
                               count(*) FILTER (
                                 WHERE status = 'claimed'
                                   AND COALESCE(last_attempt_at, updated_at, created_at)
                                       <= now() - ($1::int * interval '1 second')
                               )::int AS stale_claimed,
                               count(*) FILTER (WHERE status = 'pending')::int AS pending,
                               count(*) FILTER (WHERE status = 'failed')::int AS failed,
                               count(*) FILTER (WHERE status = 'unavailable')::int AS unavailable,
                               count(*) FILTER (WHERE status = 'completed')::int AS completed,
                               max(updated_at) AS last_seen_at
                        FROM browser_media_revisit_queue
                        GROUP BY platform
                        ORDER BY due DESC, pending DESC, failed DESC, platform
                        LIMIT 8
                        """,
                        claim_timeout,
                        timeout=10,
                    )]
            except Exception:
                pass

            try:
                if await conn.fetchval("SELECT to_regclass('tiktok_browser_media_candidates')", timeout=5) is not None:
                    snap["tiktok_browser_media_diagnostics"] = [dict(r) for r in await conn.fetch(
                        """
                        SELECT outcome,
                               count(*)::int AS candidates,
                               count(*) FILTER (WHERE needs_revisit)::int AS needs_revisit,
                               max(last_seen) AS last_seen_at
                        FROM tiktok_browser_media_candidates
                        WHERE last_seen >= date_trunc('hour', now())
                        GROUP BY outcome
                        ORDER BY candidates DESC, last_seen_at DESC
                        LIMIT 8
                        """,
                        timeout=10,
                    )]
                if await conn.fetchval("SELECT to_regclass('tiktok_browser_revisit_queue')", timeout=5) is not None:
                    claim_timeout = _tiktok_revisit_claim_timeout_seconds()
                    row = await conn.fetchrow(
                        """
                        SELECT count(*) FILTER (
                                 WHERE status IN ('pending', 'failed')
                                   AND next_visit_at <= now()
                               )::int AS due,
                               count(*) FILTER (WHERE status = 'claimed')::int AS claimed,
                               count(*) FILTER (
                                 WHERE status = 'claimed'
                                   AND COALESCE(last_attempt_at, updated_at, created_at)
                                       <= now() - ($1::int * interval '1 second')
                               )::int AS stale_claimed,
                               count(*) FILTER (WHERE status = 'pending')::int AS pending,
                               count(*) FILTER (WHERE status = 'failed')::int AS failed,
                               count(*) FILTER (WHERE status = 'unavailable')::int AS unavailable,
                               count(*) FILTER (WHERE status = 'completed')::int AS completed,
                               max(updated_at) AS last_seen_at
                        FROM tiktok_browser_revisit_queue
                        """,
                        claim_timeout,
                        timeout=10,
                    )
                    if row:
                        snap["tiktok_browser_revisit_queue"] = dict(row)
            except Exception:
                pass

            try:
                if await conn.fetchval("SELECT to_regclass('x_profile_targets')", timeout=5) is not None:
                    target_row = await conn.fetchrow(
                        """
                        SELECT count(*)::int AS targets,
                               count(*) FILTER (
                                 WHERE status IN ('pending', 'failed', 'completed')
                                   AND next_visit_at <= now()
                               )::int AS due_targets,
                               count(*) FILTER (WHERE status = 'claimed')::int AS claimed_targets,
                               count(*) FILTER (WHERE status = 'failed')::int AS failed_targets,
                               count(*) FILTER (WHERE status = 'unavailable')::int AS unavailable_targets,
                               max(last_success_at) AS last_success_at
                        FROM x_profile_targets
                        """,
                        timeout=10,
                    )
                    edge_count = 0
                    if await conn.fetchval("SELECT to_regclass('x_edges')", timeout=5) is not None:
                        edge_count = int(await conn.fetchval("SELECT count(*) FROM x_edges", timeout=10) or 0)
                    profile_hour = 0
                    if await conn.fetchval("SELECT to_regclass('x_profiles')", timeout=5) is not None:
                        profile_hour = int(await conn.fetchval(
                            "SELECT count(*) FROM x_profiles WHERE updated_at >= date_trunc('hour', now())",
                            timeout=10,
                        ) or 0)
                    posts_hour = int(await conn.fetchval(
                        "SELECT count(*) FROM x_posts WHERE collected_at >= date_trunc('hour', now())",
                        timeout=10,
                    ) or 0)
                    media_hour = int(await conn.fetchval(
                        "SELECT count(*) FROM media_items WHERE source='x' AND collected_at >= date_trunc('hour', now())",
                        timeout=10,
                    ) or 0)
                    browser_row = None
                    if await conn.fetchval("SELECT to_regclass('browser_ingest_events')", timeout=5) is not None:
                        browser_row = await conn.fetchrow(
                            """
                            SELECT coalesce(sum(observed_count), 0)::int AS observed,
                                   coalesce(sum(stored_count), 0)::int AS stored,
                                   count(*)::int AS requests,
                                   max(created_at) AS last_seen_at
                            FROM browser_ingest_events
                            WHERE platform = 'x'
                              AND created_at >= date_trunc('hour', now())
                            """,
                            timeout=10,
                        )
                    target = dict(target_row) if target_row else {}
                    browser = dict(browser_row) if browser_row else {}
                    last_success = target.get("last_success_at")
                    snap["x_collection_health"] = {
                        "targets": int(target.get("targets") or 0),
                        "due_targets": int(target.get("due_targets") or 0),
                        "claimed_targets": int(target.get("claimed_targets") or 0),
                        "failed_targets": int(target.get("failed_targets") or 0),
                        "unavailable_targets": int(target.get("unavailable_targets") or 0),
                        "edge_count": edge_count,
                        "profiles_hour": profile_hour,
                        "posts_hour": posts_hour,
                        "media_hour": media_hour,
                        "browser_observed_hour": int(browser.get("observed") or 0),
                        "browser_stored_hour": int(browser.get("stored") or 0),
                        "browser_requests_hour": int(browser.get("requests") or 0),
                        "last_profile_success_age_seconds": int(
                            (datetime.now(timezone.utc) - last_success).total_seconds()
                        ) if last_success else None,
                    }
            except Exception:
                pass

            try:
                active_limits = []
                active_sources = set()
                now_ts = datetime.now(timezone.utc).timestamp()
                for row in await conn.fetch(
                    """
                    SELECT service, last_processed_id, status
                    FROM service_cursors
                    WHERE status = 'blocked'
                      AND (service ILIKE '%rate_limit' OR service ILIKE '%ratelimit')
                    ORDER BY last_processed_at DESC NULLS LAST
                    """,
                    timeout=10,
                ):
                    raw = str(row["last_processed_id"] or "")
                    if ":" not in raw:
                        continue
                    left, right = raw.split(":", 1)
                    try:
                        expiry_ts = float(left)
                    except Exception:
                        continue
                    if expiry_ts <= now_ts:
                        continue
                    try:
                        streak = int(right)
                    except Exception:
                        streak = None
                    active_limits.append({
                        "service": row["service"],
                        "seconds_remaining": int(expiry_ts - now_ts),
                        "streak": streak,
                        "basis": "service_cursor",
                    })
                    active_sources.add(
                        str(row["service"] or "")
                        .lower()
                        .replace("_rate_limit", "")
                        .replace("_ratelimit", "")
                    )
                for row in await conn.fetch(
                    """
                    SELECT source, account, scope,
                           extract(epoch FROM max(created_at + cooldown_seconds * interval '1 second')) AS expiry_ts,
                           count(*)::int AS events,
                           max(reason) AS reason
                    FROM rate_limit_events
                    WHERE cooldown_seconds IS NOT NULL
                      AND (
                          status_code = 429
                          OR status_code IS NULL
                          OR (
                              source = 'youtube'
                              AND status_code = 403
                              AND reason = 'youtube_api_quota_or_access'
                          )
                      )
                      AND created_at + cooldown_seconds * interval '1 second' > now()
                    GROUP BY source, account, scope
                    ORDER BY expiry_ts DESC
                    LIMIT 8
                    """,
                    timeout=10,
                ):
                    source = str(row["source"] or "").lower()
                    if source in active_sources:
                        continue
                    expiry_ts = float(row["expiry_ts"] or 0)
                    if expiry_ts <= now_ts:
                        continue
                    active_limits.append({
                        "service": row["source"],
                        "account": row["account"],
                        "scope": row["scope"],
                        "seconds_remaining": int(expiry_ts - now_ts),
                        "events": int(row["events"] or 0),
                        "reason": row["reason"],
                        "basis": "rate_limit_events",
                    })
                snap["active_rate_limits"] = active_limits[:5]
            except Exception:
                pass

            # Per-source freshness from real data (accurate for realtime too).
            ages: dict[str, int] = {}
            stale: list[str] = []
            for name, query, thresh in freshness:
                try:
                    age = await conn.fetchval(query, timeout=15)
                except Exception:
                    continue
                if age is None:
                    continue  # source has no data yet — not "stale", just unseen
                ages[name] = int(age)
                if age > thresh:
                    stale.append(name)
            snap["source_ages"] = ages
            snap["stale_sources"] = sorted(stale)

            try:
                from src.core.whatsapp_bridge_health import (
                    fetch_whatsapp_bridge_health,
                    summarize_whatsapp_bridge_health,
                )
                wa_states = await fetch_whatsapp_bridge_health(timeout=4)
                snap["whatsapp_bridge_health"] = {
                    "summary": summarize_whatsapp_bridge_health(wa_states),
                    "bridges": wa_states,
                }
            except Exception as exc:
                snap["whatsapp_bridge_health"] = {
                    "summary": {
                        "status": "unreachable",
                        "detail": f"WhatsApp bridge health check failed: {exc}",
                        "ready_count": 0,
                        "reachable_count": 0,
                        "total": 2,
                    },
                    "bridges": [],
                }

            # Backfill-vs-realtime signals (for the "Backfill:" heartbeat line).
            # (a) messaging realtime %: of rows INGESTED in the last hour, how
            # many carry a message timestamp also within the hour. ~100% = caught
            # up; low = still draining history under recent collected_at.
            rt: dict[str, float] = {}
            for src, tbl, ins_col, ts_col in (
                ("telegram", "telegram_messages", "collected_at", "platform_created_at"),
                ("whatsapp", "whatsapp_messages", "collected_at", "timestamp"),
                ("beeper", "beeper_shadow_messages", "ingested_at", "timestamp"),
            ):
                try:
                    pct = await conn.fetchval(
                        f"SELECT round(100.0*count(*) FILTER "
                        f"(WHERE {ts_col} > now()-interval '1 hour') "
                        f"/ NULLIF(count(*),0),1) "
                        f"FROM {tbl} "
                        f"WHERE {ins_col} > now()-interval '1 hour'", timeout=20)
                    if pct is not None:
                        rt[src] = float(pct)
                except Exception:
                    continue
            snap["realtime_pct"] = rt

            # (b) spider-queue pending depth per source (remaining discovery/
            # backfill work). Some are true backfill (telegram dialogs); github/
            # strava are perpetual crawl frontiers that never reach 0.
            qp: dict[str, int] = {}
            for src in ("telegram", "instagram", "lemon8", "tiktok", "youtube", "github", "strava"):
                try:
                    n = await conn.fetchval(
                        f"SELECT count(*) FROM {src}_spider_queue WHERE status='pending'", timeout=15)
                    qp[src] = int(n or 0)
                except Exception:
                    continue
            snap["queue_pending"] = qp

            try:
                vh = vault_health()
                vault = {
                    "root": str(vh.root),
                    "available": vh.available,
                    "writable": vh.writable,
                    "free_bytes": vh.free_bytes,
                    "total_bytes": vh.total_bytes,
                    "error": vh.error,
                }
                try:
                    vault.update(await vault_artifact_counts(conn, timeout=5))
                except Exception as exc:
                    vault["counts_error"] = exc.__class__.__name__
                snap["vault"] = vault
            except Exception as exc:
                root = os.getenv("COLLECTOR_VAULT_ROOT") or str(VAULT_ROOT)
                snap["vault"] = {
                    "root": root,
                    "available": False,
                    "writable": False,
                    "free_bytes": None,
                    "total_bytes": None,
                    "sidecar_failures": 0,
                    "artifacts_queued": 0,
                    "artifacts_partial": 0,
                    "artifacts_quarantined": 0,
                    "error": str(exc),
                }

            snap["backups"] = backup_status()

            # Health flags from source_health (dead + degraded/auth_paused).
            # Include freshness context so the Telegram heartbeat explains
            # why a source is degraded instead of only naming it.
            try:
                stale_after = {name: thresh for name, _query, thresh in freshness}
                rows = await conn.fetch(
                    "SELECT source, status, last_error FROM source_health "
                    "WHERE status IN ('dead','degraded','auth_paused')"
                )
                dead_sources = set(r["source"] for r in rows if r["status"] == "dead")
                degraded_sources = set(
                    r["source"] for r in rows if r["status"] in ("degraded", "auth_paused"))
                degraded_details = [
                    {
                        "source": r["source"],
                        "status": r["status"],
                        "reason": r["last_error"],
                        "age_seconds": ages.get(r["source"]),
                        "stale_after_seconds": stale_after.get(r["source"]),
                    }
                    for r in rows
                    if r["status"] in ("degraded", "auth_paused")
                ]
                wa_summary = (snap.get("whatsapp_bridge_health") or {}).get("summary") or {}
                wa_status = wa_summary.get("status")
                if wa_status and wa_status != "paired":
                    degraded_sources.add("whatsapp")
                    degraded_details = [
                        row for row in degraded_details
                        if row.get("source") != "whatsapp"
                    ]
                    degraded_details.append({
                        "source": "whatsapp",
                        "status": wa_status,
                        "reason": wa_summary.get("detail"),
                        "age_seconds": ages.get("whatsapp"),
                        "stale_after_seconds": stale_after.get("whatsapp"),
                    })
                snap["dead_sources"] = sorted(dead_sources)
                snap["degraded_sources"] = sorted(degraded_sources)
                snap["degraded_details"] = degraded_details
            except Exception:
                pass

            try:
                if await conn.fetchval("SELECT to_regclass('collector_operational_events')") is not None:
                    rows = await conn.fetch(
                        """
                        SELECT e.source,
                               e.event_type,
                               e.severity,
                               e.summary,
                               e.metadata,
                               e.created_at,
                               extract(epoch FROM now() - e.created_at)::int AS age_seconds,
                               h.last_success_at,
                               CASE
                                 WHEN h.last_success_at IS NOT NULL
                                  AND h.last_success_at > e.created_at
                                 THEN true
                                 ELSE false
                               END AS resolved_by_success,
                               extract(epoch FROM now() - h.last_success_at)::int AS last_success_age_seconds
                        FROM collector_operational_events e
                        LEFT JOIN source_health h ON h.source = e.source
                        WHERE e.created_at >= now() - interval '24 hours'
                        ORDER BY e.created_at DESC
                        LIMIT 8
                        """,
                        timeout=8,
                    )
                    snap["operational_events"] = [dict(r) for r in rows]
            except Exception:
                pass
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # ok reflects REAL health: green only when nothing dead/stale and the
    # vault can safely accept file-backed artifacts.
    vault = snap.get("vault") or {}
    vault_bad = bool(vault) and (
        not vault.get("available")
        or not vault.get("writable")
        or int(vault.get("artifacts_queued") or 0) > 0
        or int(vault.get("artifacts_partial") or 0) > 0
    )
    backups = snap.get("backups") or {}
    backups_bad = bool(backups) and backups.get("status") not in {"ok", "refreshing"}
    snap["ok"] = not (snap.get("dead_sources") or snap.get("stale_sources") or vault_bad or backups_bad)
    return snap


# --- 15-minute delta snapshot -----------------------------------------------


async def build_status_delta(pool, interval_minutes: int) -> dict | None:
    """Build a small snapshot for notify_status_delta.

    Uses service_cursors with service='notify_status_delta' to persist the
    last-tick timestamp. On the first ever run (no cursor row yet) it
    seeds the cursor with (now - interval) so the very first delta message
    still reports content instead of an empty tick. Returns None ONLY
    when the persisted cursor says the last tick was less than
    ``interval_minutes`` ago (a scheduler that just restarted must not
    double-send).
    """
    interval_seconds = max(60, int(interval_minutes) * 60)
    now_dt = datetime.now(timezone.utc)
    snap: dict = {}
    try:
        async with pool.acquire() as conn:
            cursor_row = await conn.fetchrow(
                "SELECT last_processed_at FROM service_cursors "
                "WHERE service = 'notify_status_delta'",
            )
            previous_tick = None
            if cursor_row and cursor_row["last_processed_at"]:
                previous_tick = cursor_row["last_processed_at"]
                if previous_tick.tzinfo is None:
                    previous_tick = previous_tick.replace(tzinfo=timezone.utc)
            else:
                previous_tick = now_dt - timedelta(seconds=interval_seconds)

            elapsed = (now_dt - previous_tick).total_seconds()
            if elapsed < interval_seconds * 0.9:
                # Persisted cursor says another scheduler already ticked
                # recently. Skip; the monotonic gate will re-attempt in
                # a minute.
                return None

            snap["window_seconds"] = int(elapsed)
            snap["previous_tick_at"] = previous_tick.isoformat()
            snap["per_source"] = await delta_per_source_counts(conn, previous_tick)
            snap["new_cooldowns"] = await delta_new_cooldowns(conn, previous_tick)
            snap["new_dead_sources"] = await delta_new_dead_sources(conn, previous_tick)
            snap["extension_hooks"] = await delta_extension_hooks(conn)

            totals = {"posts": 0, "media": 0, "messages": 0}
            for row in snap["per_source"].values():
                totals["posts"] += int(row.get("posts", 0) or 0)
                totals["media"] += int(row.get("media", 0) or 0)
                totals["messages"] += int(row.get("messages", 0) or 0)
            totals["cooldowns"] = len(snap["new_cooldowns"])
            snap["totals"] = totals

            # Persist the new tick BEFORE returning so a crash mid-send
            # doesn't cause a duplicate on the next tick.
            await conn.execute(
                "INSERT INTO service_cursors (service, last_processed_at, status) "
                "VALUES ('notify_status_delta', $1, 'idle') "
                "ON CONFLICT (service) DO UPDATE SET last_processed_at = EXCLUDED.last_processed_at",
                now_dt,
            )
    except Exception as exc:
        snap.setdefault("error", str(exc)[:300])
    return snap


async def delta_per_source_counts(conn, since) -> dict[str, dict[str, int]]:
    """Per-source delta counts since ``since`` for posts, media, messages."""
    result: dict[str, dict[str, int]] = {}
    # Messages: telegram / whatsapp / beeper.
    message_queries = (
        ("telegram", "telegram_messages", "collected_at"),
        ("whatsapp", "whatsapp_messages", "collected_at"),
        ("beeper", "beeper_shadow_messages", "ingested_at"),
    )
    for src, tbl, col in message_queries:
        try:
            n = int(await conn.fetchval(
                f"SELECT count(*) FROM {tbl} WHERE {col} > $1",
                since, timeout=10,
            ) or 0)
        except Exception:
            continue
        if n:
            bucket = result.setdefault(src, {"posts": 0, "media": 0, "messages": 0})
            bucket["messages"] = n

    # Posts: per-platform post tables. Use the same set as _STATUS_CONTENT_PARTS
    # but only the ones that carry post-shaped rows the operator cares about.
    post_queries = (
        ("instagram", "instagram_posts", "collected_at", "posts"),
        ("tiktok", "tiktok_posts", "collected_at", "posts"),
        ("lemon8", "lemon8_posts", "collected_at", "posts"),
        ("threads", "threads_posts", "collected_at", "posts"),
        ("facebook", "facebook_posts", "collected_at", "posts"),
        ("x", "x_posts", "collected_at", "posts"),
        ("youtube", "youtube_videos", "collected_at", "posts"),
        ("github", "github_commits", "collected_at", "posts"),
        ("website", "website_pages", "collected_at", "posts"),
        ("strava", "strava_activities", "collected_at", "posts"),
        ("search", "search_results", "collected_at", "posts"),
    )
    for src, tbl, col, _label in post_queries:
        try:
            n = int(await conn.fetchval(
                f"SELECT count(*) FROM {tbl} WHERE {col} > $1",
                since, timeout=10,
            ) or 0)
        except Exception:
            continue
        if n:
            bucket = result.setdefault(src, {"posts": 0, "media": 0, "messages": 0})
            bucket["posts"] = n

    # Media across all sources.
    try:
        for row in await conn.fetch(
            "SELECT source, count(*)::int AS n FROM media_items "
            "WHERE collected_at > $1 GROUP BY source",
            since, timeout=15,
        ):
            bucket = result.setdefault(
                row["source"], {"posts": 0, "media": 0, "messages": 0},
            )
            bucket["media"] = int(row["n"] or 0)
    except Exception:
        pass
    return result


async def delta_new_cooldowns(conn, since) -> list[dict]:
    """Cooldowns that BECAME active in the delta window and are still active.

    Uses the same shape as build_status()'s active_rate_limits so the
    formatter can reuse fields, but restricted to rows created after
    ``since`` and still in cooldown.
    """
    rows: list[dict] = []
    try:
        fetched = await conn.fetch(
            """
            SELECT source, account, scope,
                   max(created_at) AS started_at,
                   extract(epoch FROM
                       max(created_at + cooldown_seconds * interval '1 second') - now()
                   )::int AS seconds_remaining,
                   count(*)::int AS events,
                   max(reason) AS reason
            FROM rate_limit_events
            WHERE created_at > $1
              AND cooldown_seconds IS NOT NULL
              AND created_at + cooldown_seconds * interval '1 second' > now()
            GROUP BY source, account, scope
            ORDER BY started_at DESC
            LIMIT 6
            """,
            since, timeout=10,
        )
    except Exception:
        return rows
    for r in fetched:
        remaining = int(r["seconds_remaining"] or 0)
        if remaining <= 0:
            continue
        rows.append({
            "service": r["source"],
            "account": r["account"],
            "scope": r["scope"],
            "seconds_remaining": remaining,
            "events": int(r["events"] or 0),
            "reason": r["reason"],
        })
    return rows


async def delta_new_dead_sources(conn, since) -> list[str]:
    """Sources with operational_events severity=fatal added in the window."""
    try:
        if await conn.fetchval("SELECT to_regclass('operational_events')", timeout=5) is None:
            return []
        fetched = await conn.fetch(
            """
            SELECT DISTINCT source
            FROM operational_events
            WHERE created_at > $1
              AND severity IN ('fatal', 'critical')
              AND COALESCE(resolved_at, 'infinity'::timestamptz) > now()
            """,
            since, timeout=10,
        )
    except Exception:
        return []
    return [str(r["source"]) for r in fetched if r["source"]]


async def delta_extension_hooks(conn) -> list[dict]:
    """Compact per-platform extension hook state for the delta 1-liner."""
    try:
        if await conn.fetchval("SELECT to_regclass('dm_hook_heartbeat')", timeout=5) is None:
            return []
        fetched = await conn.fetch(
            """
            SELECT platform,
                   extract(epoch FROM now() - max(last_seen))::int AS age_seconds,
                   (array_agg(extension_version ORDER BY last_seen DESC))[1] AS extension_version
            FROM dm_hook_heartbeat
            GROUP BY platform
            """,
            timeout=10,
        )
    except Exception:
        return []
    return [
        {
            "platform": str(r["platform"]),
            "age_seconds": int(r["age_seconds"] or 0),
            "extension_version": r["extension_version"],
        }
        for r in fetched
    ]


__all__ = [
    "build_status",
    "build_status_delta",
    "delta_per_source_counts",
    "delta_new_cooldowns",
    "delta_new_dead_sources",
    "delta_extension_hooks",
]
