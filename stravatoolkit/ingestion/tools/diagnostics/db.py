from __future__ import annotations

from ingestion.config import load_settings
from ingestion.db import connect, init_db


def check_backfill_health():
    """Check backfill health and report on degraded/stuck athletes."""
    settings = load_settings()
    init_db(settings.db_path)
    conn = connect(settings.db_path)

    try:
        overview = conn.execute("""
            SELECT
                COUNT(*) as total_athletes,
                SUM(CASE WHEN is_tracked = 1 THEN 1 ELSE 0 END) as tracked_athletes,
                SUM(CASE WHEN backfill_status = 'degraded' THEN 1 ELSE 0 END) as degraded_athletes,
                SUM(CASE WHEN backfill_completed_at IS NOT NULL THEN 1 ELSE 0 END) as completed_athletes,
                SUM(CASE WHEN backfill_status = 'active' OR backfill_status = 'gap' THEN 1 ELSE 0 END) as in_progress_athletes
            FROM athletes
        """).fetchone()

        print("=" * 70)
        print("BACKFILL HEALTH CHECK")
        print("=" * 70)
        print(f"\nTotal athletes: {overview['total_athletes']}")
        print(f"Tracked: {overview['tracked_athletes']}")
        print(f"  - Completed: {overview['completed_athletes']}")
        print(f"  - In progress: {overview['in_progress_athletes']}")
        print(f"  - Degraded: {overview['degraded_athletes']}")

        degraded = conn.execute("""
            SELECT
                at.athlete_id,
                at.name,
                at.backfill_deep_cursor_before,
                at.backfill_status,
                at.backfill_last_issue_code,
                at.backfill_last_issue_message,
                at.backfill_last_issue_at,
                COUNT(a.activity_id) as activity_count
            FROM athletes at
            LEFT JOIN activities a ON at.athlete_id = a.athlete_id
            WHERE at.is_tracked = 1 AND at.backfill_status = 'degraded'
            GROUP BY at.athlete_id
            ORDER BY at.backfill_last_issue_at DESC
        """).fetchall()

        if degraded:
            print(f"\n{'=' * 70}")
            print(f"DEGRADED ATHLETES ({len(degraded)})")
            print(f"{'=' * 70}")
            print(f"\nNote: Degraded athletes will automatically advance on next backfill run.\n")

            for athlete in degraded:
                print(f"  {athlete['name']} (ID: {athlete['athlete_id']})")
                print(f"    - Stuck on: {athlete['backfill_deep_cursor_before']}")
                print(f"    - Issue: {athlete['backfill_last_issue_code']}")
                print(f"    - Activities: {athlete['activity_count']}")
                print()
        else:
            print(f"\n✓ No degraded athletes found!")

        return {
            "total_athletes": overview["total_athletes"],
            "tracked_athletes": overview["tracked_athletes"],
            "completed_athletes": overview["completed_athletes"],
            "in_progress_athletes": overview["in_progress_athletes"],
            "degraded_athletes": overview["degraded_athletes"],
            "degraded_details": [dict(row) for row in degraded],
        }

    finally:
        conn.close()


if __name__ == "__main__":
    check_backfill_health()
