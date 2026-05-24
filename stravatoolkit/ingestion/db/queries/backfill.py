"""Backfill-related database queries."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from ingestion.config import now_utc_iso

RECENT_CHECK_STALE_DAYS = 7


def reset_athlete_backfill(conn: sqlite3.Connection, athlete_id: int | None = None) -> int:
    """Reset backfill state to pending for one athlete (by ID) or all tracked athletes.

    Clears all cursors, issue codes, and completion timestamps so the next
    backfill run treats the athlete as freshly tracked.

    Args:
        conn: Database connection.
        athlete_id: If provided, resets only this athlete. If None, resets all tracked athletes.

    Returns:
        Number of athlete rows updated.
    """
    if athlete_id is not None:
        cursor = conn.execute(
            """
            UPDATE athletes
            SET backfill_status = 'pending',
                backfill_deep_cursor_before = NULL,
                backfill_recent_cursor_before = NULL,
                backfill_oldest_seen_utc = NULL,
                backfill_completed_at = NULL,
                backfill_recent_completed_at = NULL,
                backfill_last_coverage_check_at = NULL,
                last_crawl_status = NULL,
                backfill_last_issue_code = NULL,
                backfill_last_issue_message = NULL,
                backfill_last_issue_at = NULL
            WHERE athlete_id = ?
            """,
            (athlete_id,),
        )
    else:
        cursor = conn.execute(
            """
            UPDATE athletes
            SET backfill_status = 'pending',
                backfill_deep_cursor_before = NULL,
                backfill_recent_cursor_before = NULL,
                backfill_oldest_seen_utc = NULL,
                backfill_completed_at = NULL,
                backfill_recent_completed_at = NULL,
                backfill_last_coverage_check_at = NULL,
                last_crawl_status = NULL,
                backfill_last_issue_code = NULL,
                backfill_last_issue_message = NULL,
                backfill_last_issue_at = NULL
            WHERE is_tracked = 1
            """
        )
    return int(cursor.rowcount or 0)


def get_following_backfill_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    stale_before = (datetime.now(UTC) - timedelta(days=RECENT_CHECK_STALE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return conn.execute(
        """
        SELECT * FROM athletes
        WHERE is_tracked = 1
          AND COALESCE(backfill_status, 'pending') NOT IN ('paused', 'degraded')
          AND (
                backfill_completed_at IS NULL
                OR backfill_last_coverage_check_at IS NULL
                OR backfill_last_coverage_check_at < ?
          )
        ORDER BY
            CASE
                WHEN backfill_completed_at IS NULL THEN 0
                WHEN backfill_last_coverage_check_at IS NULL OR backfill_last_coverage_check_at < ? THEN 1
                ELSE 2
            END,
            COALESCE(backfill_oldest_seen_utc, '9999-12-31T00:00:00+00:00') DESC,
            roster_refreshed_at DESC,
            athlete_id ASC
        """,
        (stale_before, stale_before),
    ).fetchall()


def update_backfill_progress(
    conn: sqlite3.Connection,
    athlete_id: int,
    *,
    cursor_before: str | None,
    oldest_seen_utc: str | None,
    status: str,
    completed: bool = False,
    issue_code: str | None = None,
    issue_message: str | None = None,
    issue_at: str | None = None,
    phase: str = "deep",
    recent_cursor_before: str | None = None,
    recent_completed: bool | None = None,
    coverage_checked_at: str | None = None,
) -> None:
    issue_active = int(status == "degraded")
    phase_is_recent = phase == "recent"
    recent_cursor_value = recent_cursor_before if phase_is_recent else None
    deep_cursor_value = cursor_before if not phase_is_recent else None
    deep_completed_at = now_utc_iso() if completed and not phase_is_recent else None
    recent_completed_at = now_utc_iso() if recent_completed else None
    conn.execute(
        """
        UPDATE athletes
        SET backfill_recent_cursor_before = CASE
                WHEN ? = 1 THEN ?
                ELSE backfill_recent_cursor_before
            END,
            backfill_deep_cursor_before = CASE
                WHEN ? = 1 THEN backfill_deep_cursor_before
                ELSE ?
            END,
            backfill_oldest_seen_utc = COALESCE(?, backfill_oldest_seen_utc),
            backfill_status = ?,
            backfill_completed_at = CASE WHEN ? = 1 THEN ? ELSE backfill_completed_at END,
            backfill_recent_completed_at = CASE
                WHEN ? = 1 THEN ?
                ELSE backfill_recent_completed_at
            END,
            backfill_last_coverage_check_at = COALESCE(?, backfill_last_coverage_check_at),
            last_crawl_status = ?,
            backfill_last_issue_code = CASE WHEN ? = 1 THEN ? ELSE NULL END,
            backfill_last_issue_message = CASE WHEN ? = 1 THEN ? ELSE NULL END,
            backfill_last_issue_at = CASE WHEN ? = 1 THEN ? ELSE NULL END
        WHERE athlete_id = ?
        """,
        (
            int(phase_is_recent),
            recent_cursor_value,
            int(phase_is_recent),
            deep_cursor_value,
            oldest_seen_utc,
            status,
            int(completed and not phase_is_recent),
            deep_completed_at,
            int(bool(recent_completed)),
            recent_completed_at,
            coverage_checked_at,
            status,
            issue_active,
            issue_code,
            issue_active,
            issue_message,
            issue_active,
            issue_at or now_utc_iso(),
            athlete_id,
        ),
    )


def record_backfill_day_coverage(
    conn: sqlite3.Connection,
    athlete_id: int,
    calendar_date: str,
    coverage_status: str,
    *,
    source_month: str | None = None,
    checked_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO backfill_day_coverage (
            athlete_id,
            calendar_date,
            coverage_status,
            source_month,
            last_checked_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(athlete_id, calendar_date) DO UPDATE SET
            coverage_status = excluded.coverage_status,
            source_month = COALESCE(excluded.source_month, backfill_day_coverage.source_month),
            last_checked_at = excluded.last_checked_at
        """,
        (athlete_id, calendar_date, coverage_status, source_month, checked_at or now_utc_iso()),
    )


def create_crawl_run(
    conn: sqlite3.Connection,
    run_type: str,
    target_date: str | None,
    roster_refreshed: bool,
    backfill_step_limit: int,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO crawl_runs (run_type, target_date, started_at, roster_refreshed, backfill_budget_minutes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_type, target_date, now_utc_iso(), int(roster_refreshed), backfill_step_limit),
    )
    return int(cursor.lastrowid)


def finalize_crawl_run(conn: sqlite3.Connection, run_id: int, status: str, notes: str | None = None) -> None:
    conn.execute(
        "UPDATE crawl_runs SET completed_at = ?, status = ?, notes = ? WHERE id = ?",
        (now_utc_iso(), status, notes, run_id),
    )


def get_backfill_coverage(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT
            substr(a.calendar_date, 1, 4) AS year,
            substr(a.calendar_date, 1, 7) AS month,
            COUNT(*) AS activity_count,
            COUNT(DISTINCT a.athlete_id) AS athlete_count,
            SUM(CASE WHEN a.stream_status = 'ok' THEN 1 ELSE 0 END) AS ready_count
        FROM activities a
        JOIN athletes at ON at.athlete_id = a.athlete_id
        WHERE at.is_tracked = 1
        GROUP BY year, month
        ORDER BY month DESC
        """
    ).fetchall()

    years: dict[str, dict] = {}
    total_activities = 0
    total_ready = 0
    for row in rows:
        year = str(row["year"])
        if year not in years:
            years[year] = {
                "year": year,
                "activity_count": 0,
                "athlete_count": 0,
                "months": [],
            }
        month_payload = {
            "month": str(row["month"]),
            "activity_count": int(row["activity_count"] or 0),
            "athlete_count": int(row["athlete_count"] or 0),
            "ready_count": int(row["ready_count"] or 0),
        }
        years[year]["months"].append(month_payload)
        years[year]["activity_count"] += month_payload["activity_count"]
        years[year]["athlete_count"] = max(years[year]["athlete_count"], month_payload["athlete_count"])
        total_activities += month_payload["activity_count"]
        total_ready += month_payload["ready_count"]

    ordered_years = [years[year] for year in sorted(years.keys(), reverse=True)]
    return {
        "year_count": len(ordered_years),
        "month_count": len(rows),
        "activity_count": total_activities,
        "ready_count": total_ready,
        "years": ordered_years,
    }
