from __future__ import annotations

from datetime import timedelta
from typing import Any


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

    snapshots = []
    for item_source in sorted(sources):
        latest_data_at = None
        media_24h = rows_24h = 0
        if await _table_exists(conn, "media_items"):
            row = await conn.fetchrow(
                """
                SELECT MAX(collected_at) AS latest_data_at,
                       COUNT(*) FILTER (WHERE collected_at >= NOW() - INTERVAL '24 hours') AS media_24h
                FROM media_items
                WHERE source = $1
                """,
                item_source,
            )
            latest_data_at = row["latest_data_at"]
            media_24h = int(row["media_24h"] or 0)
            rows_24h += media_24h

        latest_run_at = None
        errors_24h = 0
        if await _table_exists(conn, "collection_runs"):
            row = await conn.fetchrow(
                """
                SELECT MAX(COALESCE(completed_at, started_at)) AS latest_run_at,
                       COUNT(*) FILTER (
                         WHERE started_at >= NOW() - INTERVAL '24 hours'
                           AND status NOT IN ('success', 'completed')
                       ) AS errors_24h
                FROM collection_runs
                WHERE source = $1
                """,
                item_source,
            )
            latest_run_at = row["latest_run_at"]
            errors_24h = int(row["errors_24h"] or 0)

        source_status = None
        if await _table_exists(conn, "source_health"):
            source_status = await conn.fetchval("SELECT status FROM source_health WHERE source = $1", item_source)

        rate_limits_24h = 0
        if await _table_exists(conn, "rate_limit_events"):
            rate_limits_24h = int(await conn.fetchval(
                "SELECT COUNT(*) FROM rate_limit_events WHERE source = $1 AND created_at >= NOW() - INTERVAL '24 hours'",
                item_source,
            ) or 0)

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
        }
        snapshots.append(snapshot)

    if snapshots and write:
        await conn.executemany(
            """
            INSERT INTO collection_coverage_snapshots (
                source, expected_cadence, latest_data_at, latest_run_at, status,
                rows_24h, media_24h, errors_24h, rate_limits_24h,
                private_access_failures, stale_targets, created_at
            )
            VALUES ($1, $2::interval, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, NOW())
            """,
            [
                (
                    row["source"], row["expected_cadence"], row["latest_data_at"],
                    row["latest_run_at"], row["status"], row["rows_24h"],
                    row["media_24h"], row["errors_24h"], row["rate_limits_24h"],
                    row["private_access_failures"], "[]",
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
        },
        "written": len(snapshots) if write else 0,
    }
