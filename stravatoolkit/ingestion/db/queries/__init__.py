"""Database query modules."""
from __future__ import annotations

from ingestion.db.queries.activities import (
    activity_exists_with_terminal_stream,
    list_activity_photo_targets,
    local_calendar_date_iso,
    mark_activity_photo_downloaded,
    reset_activity_stream_status,
    save_activity,
    save_activity_photos,
)
from ingestion.db.queries.athletes import (
    build_athlete_route_history,
    get_athlete_detail,
    list_athletes,
    sync_following_roster,
    upsert_athlete,
)
from ingestion.db.queries.backfill import (
    create_crawl_run,
    finalize_crawl_run,
    get_backfill_coverage,
    get_following_backfill_candidates,
    record_backfill_day_coverage,
    reset_athlete_backfill,
    update_backfill_progress,
)
from ingestion.db.queries.integrity import check_db_integrity
from ingestion.db.queries.photos import (
    get_latest_profile_photo,
    insert_profile_photo_history,
    list_profile_photo_targets,
    touch_profile_photo_history,
)
from ingestion.db.queries.playback import build_day_playback, list_available_dates
from ingestion.db.queries.status import get_status_summary
from ingestion.db.queries.explore import (
    list_explore_segments,
    list_explore_stubs,
    promote_explore_athletes,
    save_explore_segment,
)

__all__ = [
    "upsert_athlete",
    "sync_following_roster",
    "list_athletes",
    "get_athlete_detail",
    "build_athlete_route_history",
    "activity_exists_with_terminal_stream",
    "save_activity",
    "save_activity_photos",
    "local_calendar_date_iso",
    "reset_activity_stream_status",
    "list_activity_photo_targets",
    "mark_activity_photo_downloaded",
    "get_following_backfill_candidates",
    "update_backfill_progress",
    "record_backfill_day_coverage",
    "reset_athlete_backfill",
    "create_crawl_run",
    "finalize_crawl_run",
    "get_backfill_coverage",
    "list_profile_photo_targets",
    "get_latest_profile_photo",
    "insert_profile_photo_history",
    "touch_profile_photo_history",
    "list_available_dates",
    "build_day_playback",
    "get_status_summary",
    "check_db_integrity",
    "list_explore_stubs",
    "promote_explore_athletes",
    "save_explore_segment",
    "list_explore_segments",
]
