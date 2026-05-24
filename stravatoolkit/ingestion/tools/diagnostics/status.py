from __future__ import annotations

import json

from ingestion import db
from ingestion.config import load_settings


def main() -> None:
    settings = load_settings()
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        summary = db.get_status_summary(conn)
        dates = db.list_available_dates(conn)[:5]
        latest_run = conn.execute(
            """
            SELECT run_type, target_date, started_at, completed_at, status
            FROM crawl_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    payload = {
        "tracked_athletes": summary["tracked_roster_size"],
        "currently_following": summary["follow_roster_size"],
        "activities": summary["activity_count"],
        "last_successful_sync_date": summary["last_successful_sync_date"],
        "backfill_completed": summary["backfill_completed"],
        "backfill_pending": summary["backfill_pending"],
        "recent_dates": dates,
        "latest_run": dict(latest_run) if latest_run else None,
    }
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
