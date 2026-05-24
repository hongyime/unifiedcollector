"""Database package - re-exports all public functions for backward compatibility."""
from __future__ import annotations

# Re-export connection functions
from ingestion.db.connection import (
    checkpoint,
    connect,
    connect_readonly,
    init_db,
    repair_backfill_state,
    save_session_state,
    transaction,
)

# Re-export schema
from ingestion.db.schema import SCHEMA_SQL

# Re-export color utilities
from ingestion.db.colors import activity_palette, athlete_color, hsl_to_rgb

# Re-export all query functions
from ingestion.db.queries import (
    activity_exists_with_terminal_stream,
    build_athlete_route_history,
    build_day_playback,
    check_db_integrity,
    create_crawl_run,
    finalize_crawl_run,
    get_athlete_detail,
    get_backfill_coverage,
    get_following_backfill_candidates,
    get_latest_profile_photo,
    get_status_summary,
    insert_profile_photo_history,
    list_activity_photo_targets,
    list_athletes,
    list_available_dates,
    list_explore_segments,
    list_explore_stubs,
    list_profile_photo_targets,
    local_calendar_date_iso,
    mark_activity_photo_downloaded,
    promote_explore_athletes,
    record_backfill_day_coverage,
    reset_athlete_backfill,
    reset_activity_stream_status,
    save_activity,
    save_activity_photos,
    save_explore_segment,
    sync_following_roster,
    touch_profile_photo_history,
    update_backfill_progress,
    upsert_athlete,
)

__all__ = [
    # Connection
    "checkpoint",
    "connect",
    "connect_readonly",
    "init_db",
    "repair_backfill_state",
    "save_session_state",
    "transaction",
    # Schema
    "SCHEMA_SQL",
    # Athletes
    "upsert_athlete",
    "sync_following_roster",
    "get_following_backfill_candidates",
    "update_backfill_progress",
    "list_athletes",
    "get_athlete_detail",
    "build_athlete_route_history",
    "athlete_color",
    # Activities
    "activity_exists_with_terminal_stream",
    "save_activity",
    "save_activity_photos",
    "local_calendar_date_iso",
    "activity_palette",
    "reset_activity_stream_status",
    "list_activity_photo_targets",
    "mark_activity_photo_downloaded",
    # Playback
    "list_available_dates",
    "build_day_playback",
    # Backfill
    "create_crawl_run",
    "finalize_crawl_run",
    "get_backfill_coverage",
    "record_backfill_day_coverage",
    "reset_athlete_backfill",
    # Photos
    "list_profile_photo_targets",
    "get_latest_profile_photo",
    "insert_profile_photo_history",
    "touch_profile_photo_history",
    # Status
    "get_status_summary",
    # Integrity
    "check_db_integrity",
    # Colors
    "hsl_to_rgb",
    # Explore
    "list_explore_stubs",
    "promote_explore_athletes",
    "save_explore_segment",
    "list_explore_segments",
]
