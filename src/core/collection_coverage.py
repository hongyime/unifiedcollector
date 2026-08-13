from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from src.core.seen_targets import refresh_seen_targets_from_sources, seen_target_summary_by_source


async def _table_exists(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table))


async def _add_sources_from_table(conn, sources: set[str], table: str, column: str, source: str | None) -> None:
    if not await _table_exists(conn, table):
        return
    rows = await conn.fetch(
        f"SELECT DISTINCT {column} AS source FROM {table} WHERE ($1::text IS NULL OR {column} = $1)",
        source,
    )
    sources.update(str(row["source"]) for row in rows if row["source"])


def _status(latest_data_at, latest_run_at, source_status: str | None, expected_cadence: timedelta) -> str:
    if source_status in {"dead", "failed"}:
        return "stale"
    if source_status == "degraded":
        return "degraded"
    latest = latest_data_at or latest_run_at
    if latest is None:
        return "unknown"
    from datetime import datetime, timezone

    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - latest
    if age > expected_cadence * 2:
        return "stale"
    if age > expected_cadence:
        return "degraded"
    return "fresh"


async def build_collection_coverage_snapshot(
    conn,
    *,
    expected_cadence_hours: int = 24,
    source: str | None = None,
    write: bool = True,
) -> dict[str, Any]:
    expected = timedelta(hours=expected_cadence_hours)
    sources: set[str] = set()
    await _add_sources_from_table(conn, sources, "source_health", "source", source)
    await _add_sources_from_table(conn, sources, "service_cursors", "service", source)
    await _add_sources_from_table(conn, sources, "collection_schedules", "source", source)
    await _add_sources_from_table(conn, sources, "media_source_rollups", "source", source)
    if not sources:
        await _add_sources_from_table(conn, sources, "collection_runs", "source", source)

    media_rollups: dict[str, Any] = {}
    if await _table_exists(conn, "media_source_rollups"):
        rows = await conn.fetch(
            """
            SELECT source, latest_media_at
            FROM media_source_rollups
            WHERE ($1::text IS NULL OR source = $1)
            """,
            source,
        )
        for row in rows:
            media_rollups[str(row["source"])] = row["latest_media_at"]

    media_recent: dict[str, dict[str, Any]] = {}
    if await _table_exists(conn, "media_items"):
        rows = await conn.fetch(
            """
            SELECT source,
                   MAX(collected_at) AS latest_recent_at,
                   COUNT(*)::bigint AS media_24h
            FROM media_items
            WHERE collected_at >= NOW() - INTERVAL '24 hours'
              AND ($1::text IS NULL OR source = $1)
            GROUP BY source
            """,
            source,
        )
        for row in rows:
            media_recent[str(row["source"])] = {
                "latest_recent_at": row["latest_recent_at"],
                "media_24h": int(row["media_24h"] or 0),
            }

    run_stats: dict[str, dict[str, Any]] = {}
    if await _table_exists(conn, "collection_runs"):
        rows = await conn.fetch(
            """
            SELECT source,
                   MAX(COALESCE(completed_at, started_at)) AS latest_run_at,
                   COUNT(*) FILTER (
                     WHERE started_at >= NOW() - INTERVAL '24 hours'
                       AND status NOT IN ('success', 'completed')
                   )::bigint AS errors_24h
            FROM collection_runs
            WHERE ($1::text IS NULL OR source = $1)
            GROUP BY source
            """,
            source,
        )
        for row in rows:
            run_stats[str(row["source"])] = {
                "latest_run_at": row["latest_run_at"],
                "errors_24h": int(row["errors_24h"] or 0),
            }

    source_health: dict[str, str | None] = {}
    if await _table_exists(conn, "source_health"):
        rows = await conn.fetch(
            "SELECT source, status FROM source_health WHERE ($1::text IS NULL OR source = $1)",
            source,
        )
        source_health = {str(row["source"]): row["status"] for row in rows}

    rate_limits: dict[str, int] = {}
    if await _table_exists(conn, "rate_limit_events"):
        rows = await conn.fetch(
            """
            SELECT source, COUNT(*)::bigint AS rate_limits_24h
            FROM rate_limit_events
            WHERE created_at >= NOW() - INTERVAL '24 hours'
              AND ($1::text IS NULL OR source = $1)
            GROUP BY source
            """,
            source,
        )
        rate_limits = {str(row["source"]): int(row["rate_limits_24h"] or 0) for row in rows}

    seen_targets: dict[str, dict[str, int]] = {}
    await _add_sources_from_table(conn, sources, "collector_seen_targets", "platform", source)
    if os.getenv("COLLECTOR_SEEN_TARGETS_REFRESH_ON_COVERAGE", "false").lower() in {"1", "true", "yes", "on"}:
        try:
            limit = int(os.getenv("COLLECTOR_SEEN_TARGETS_REFRESH_LIMIT", "5000"))
            await refresh_seen_targets_from_sources(conn, source=source, limit_per_source=limit)
        except Exception:
            # Coverage snapshots must keep working even if an optional source table
            # has drifted. The dashboard will show zero seen-target counts.
            pass
    try:
        seen_targets = await seen_target_summary_by_source(conn, source=source)
        sources.update(seen_targets)
    except Exception:
        seen_targets = {}

    snapshots = []
    for item_source in sorted(sources):
        recent = media_recent.get(item_source, {})
        run = run_stats.get(item_source, {})
        latest_data_at = media_rollups.get(item_source) or recent.get("latest_recent_at")
        media_24h = int(recent.get("media_24h") or 0)
        rows_24h = media_24h
        latest_run_at = run.get("latest_run_at")
        errors_24h = int(run.get("errors_24h") or 0)
        source_status = source_health.get(item_source)
        rate_limits_24h = rate_limits.get(item_source, 0)
        seen = seen_targets.get(item_source, {})

        status = _status(latest_data_at, latest_run_at, source_status, expected)
        snapshot = {
            "source": item_source,
            "expected_cadence": expected,
            "latest_data_at": latest_data_at,
            "latest_run_at": latest_run_at,
            "status": status,
            "rows_24h": rows_24h,
            "media_24h": media_24h,
            "errors_24h": errors_24h,
            "rate_limits_24h": rate_limits_24h,
            "private_access_failures": 0,
            "stale_targets": [],
            "seen_targets_total": int(seen.get("total", 0) or 0),
            "seen_targets_backfilled": int(seen.get("backfilled", 0) or 0),
            "seen_targets_pending": int(seen.get("pending", 0) or 0),
            "seen_targets_fresh": int(seen.get("fresh", 0) or 0),
            "seen_targets_stale": int(seen.get("stale", 0) or 0),
            "seen_targets_newly_discovered": int(seen.get("newly_discovered", 0) or 0),
        }
        snapshots.append(snapshot)

    if snapshots and write:
        await conn.executemany(
            """
            INSERT INTO collection_coverage_snapshots (
                source, expected_cadence, latest_data_at, latest_run_at, status,
                rows_24h, media_24h, errors_24h, rate_limits_24h,
                private_access_failures, stale_targets,
                seen_targets_total, seen_targets_backfilled, seen_targets_pending,
                seen_targets_fresh, seen_targets_stale, seen_targets_newly_discovered,
                created_at
            )
            VALUES (
                $1, $2::interval, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb,
                $12, $13, $14, $15, $16, $17, NOW()
            )
            """,
            [
                (
                    row["source"], row["expected_cadence"], row["latest_data_at"],
                    row["latest_run_at"], row["status"], row["rows_24h"],
                    row["media_24h"], row["errors_24h"], row["rate_limits_24h"],
                    row["private_access_failures"], "[]",
                    row["seen_targets_total"], row["seen_targets_backfilled"],
                    row["seen_targets_pending"], row["seen_targets_fresh"],
                    row["seen_targets_stale"], row["seen_targets_newly_discovered"],
                )
                for row in snapshots
            ],
        )

    fresh = sum(1 for row in snapshots if row["status"] == "fresh")
    degraded = sum(1 for row in snapshots if row["status"] == "degraded")
    stale = sum(1 for row in snapshots if row["status"] == "stale")
    return {
        "sources": snapshots,
        "summary": {
            "total": len(snapshots),
            "fresh": fresh,
            "degraded": degraded,
            "stale": stale,
            "digest": f"Coverage: {fresh}/{len(snapshots)} sources fresh, {degraded} degraded, {stale} stale; "
                      f"{sum(row['media_24h'] for row in snapshots)} media rows 24h.",
            "seen_targets_total": sum(row["seen_targets_total"] for row in snapshots),
            "seen_targets_pending": sum(row["seen_targets_pending"] for row in snapshots),
            "seen_targets_backfilled": sum(row["seen_targets_backfilled"] for row in snapshots),
        },
        "written": len(snapshots) if write else 0,
    }
