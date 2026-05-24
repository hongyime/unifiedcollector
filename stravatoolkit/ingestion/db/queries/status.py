"""Status and reporting database queries."""
from __future__ import annotations

import sqlite3


def get_status_summary(conn: sqlite3.Connection) -> dict:
    overview = conn.execute(
        """
        SELECT
            COUNT(*) AS athlete_count,
            SUM(CASE WHEN is_following = 1 THEN 1 ELSE 0 END) AS follow_roster_size,
            SUM(CASE WHEN is_tracked = 1 THEN 1 ELSE 0 END) AS tracked_roster_size,
            SUM(CASE WHEN is_tracked = 1 AND backfill_completed_at IS NOT NULL THEN 1 ELSE 0 END) AS backfill_completed,
            SUM(CASE WHEN is_tracked = 1 AND backfill_completed_at IS NULL THEN 1 ELSE 0 END) AS backfill_pending,
            SUM(CASE WHEN is_tracked = 1 AND backfill_status = 'degraded' THEN 1 ELSE 0 END) AS backfill_degraded,
            SUM(CASE WHEN is_tracked = 1 AND backfill_status = 'needs_endpoint' THEN 1 ELSE 0 END) AS backfill_needs_endpoint
        FROM athletes
        """
    ).fetchone()
    activity_count = conn.execute("SELECT COUNT(*) AS count FROM activities").fetchone()["count"]
    last_sync = conn.execute(
        """
        SELECT target_date
        FROM crawl_runs
        WHERE run_type = 'daily_sync' AND status = 'ok'
        ORDER BY completed_at DESC
        LIMIT 1
        """
    ).fetchone()
    return {
        "athlete_count": int(overview["athlete_count"] or 0),
        "activity_count": int(activity_count or 0),
        "follow_roster_size": int(overview["follow_roster_size"] or 0),
        "tracked_roster_size": int(overview["tracked_roster_size"] or 0),
        "backfill_completed": int(overview["backfill_completed"] or 0),
        "backfill_pending": int(overview["backfill_pending"] or 0),
        "backfill_degraded": int(overview["backfill_degraded"] or 0),
        "backfill_needs_endpoint": int(overview["backfill_needs_endpoint"] or 0),
        "last_successful_sync_date": last_sync["target_date"] if last_sync else None,
    }
