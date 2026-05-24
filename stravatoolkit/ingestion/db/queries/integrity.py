"""Database integrity check queries."""
from __future__ import annotations

import sqlite3


def check_db_integrity(conn: sqlite3.Connection) -> dict:
    """Check database integrity and return a report of issues found.

    Returns a dict with keys:
        orphaned_activities: count of activities with no matching athlete
        orphaned_streams: count of streams with no matching activity
        invalid_fk: count of invalid foreign key references (across all FK relationships)
        null_violations: count of rows with NULL in NOT NULL columns
        issues: list of human-readable issue descriptions
    """
    issues: list[str] = []

    # --- Orphaned activities (no matching athlete) ---
    orphaned_activities_rows = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM activities a
        LEFT JOIN athletes at ON at.athlete_id = a.athlete_id
        WHERE at.athlete_id IS NULL
        """
    ).fetchone()
    orphaned_activities = int(orphaned_activities_rows["cnt"] or 0)
    if orphaned_activities:
        issues.append(f"{orphaned_activities} activity record(s) reference athlete_id values not found in athletes table")

    # --- Orphaned streams (no matching activity) ---
    orphaned_streams_rows = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM streams s
        LEFT JOIN activities a ON a.activity_id = s.activity_id
        WHERE a.activity_id IS NULL
        """
    ).fetchone()
    orphaned_streams = int(orphaned_streams_rows["cnt"] or 0)
    if orphaned_streams:
        issues.append(f"{orphaned_streams} stream record(s) reference activity_id values not found in activities table")

    # --- Invalid FK: athlete_photo_history -> athletes ---
    aph_fk_rows = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM athlete_photo_history aph
        LEFT JOIN athletes at ON at.athlete_id = aph.athlete_id
        WHERE at.athlete_id IS NULL
        """
    ).fetchone()
    aph_fk = int(aph_fk_rows["cnt"] or 0)

    # --- Invalid FK: activity_photos -> athletes ---
    ap_fk_rows = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM activity_photos ap
        LEFT JOIN athletes at ON at.athlete_id = ap.athlete_id
        WHERE at.athlete_id IS NULL
        """
    ).fetchone()
    ap_fk = int(ap_fk_rows["cnt"] or 0)

    invalid_fk = orphaned_activities + orphaned_streams + aph_fk + ap_fk
    if aph_fk:
        issues.append(f"{aph_fk} athlete_photo_history record(s) reference athlete_id values not found in athletes table")
    if ap_fk:
        issues.append(f"{ap_fk} activity_photos record(s) reference athlete_id values not found in athletes table")

    # --- NULL violations in NOT NULL columns ---
    null_violations = 0

    # athletes NOT NULL columns (excluding PRIMARY KEY)
    athletes_not_null = [
        ("name", "TEXT"),
        ("is_private", "INTEGER"),
        ("is_following", "INTEGER"),
        ("is_tracked", "INTEGER"),
        ("first_seen_source", "TEXT"),
        ("first_seen_at", "TEXT"),
        ("last_seen_at", "TEXT"),
        ("backfill_status", "TEXT"),
    ]
    for col, _ in athletes_not_null:
        cnt = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM athletes WHERE {col} IS NULL"
        ).fetchone()["cnt"] or 0
        if cnt:
            null_violations += int(cnt)
            issues.append(f"{cnt} athletes row(s) have NULL in NOT NULL column '{col}'")

    # activities NOT NULL columns
    activities_not_null = [
        "athlete_id", "sport_type", "source",
        "start_date_utc", "start_date_local", "calendar_date",
        "privacy_zone_start", "privacy_zone_end",
        "stream_status", "ingested_at",
    ]
    for col in activities_not_null:
        cnt = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM activities WHERE {col} IS NULL"
        ).fetchone()["cnt"] or 0
        if cnt:
            null_violations += int(cnt)
            issues.append(f"{cnt} activities row(s) have NULL in NOT NULL column '{col}'")

    # streams NOT NULL columns
    streams_not_null = ["activity_id", "point_index", "longitude", "latitude", "abs_unix_ts"]
    for col in streams_not_null:
        cnt = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM streams WHERE {col} IS NULL"
        ).fetchone()["cnt"] or 0
        if cnt:
            null_violations += int(cnt)
            issues.append(f"{cnt} streams row(s) have NULL in NOT NULL column '{col}'")

    # session_state NOT NULL columns
    session_not_null = ["cookie_value", "auth_mode", "captured_at", "is_active"]
    for col in session_not_null:
        cnt = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM session_state WHERE {col} IS NULL"
        ).fetchone()["cnt"] or 0
        if cnt:
            null_violations += int(cnt)
            issues.append(f"{cnt} session_state row(s) have NULL in NOT NULL column '{col}'")

    # --- Invalid data types / ranges ---
    # Latitude must be in [-90, 90], longitude in [-180, 180]
    invalid_lat = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM streams
        WHERE latitude < -90 OR latitude > 90
        """
    ).fetchone()["cnt"] or 0
    if invalid_lat:
        issues.append(f"{invalid_lat} streams row(s) have latitude out of range [-90, 90]")

    invalid_lon = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM streams
        WHERE longitude < -180 OR longitude > 180
        """
    ).fetchone()["cnt"] or 0
    if invalid_lon:
        issues.append(f"{invalid_lon} streams row(s) have longitude out of range [-180, 180]")

    # abs_unix_ts should be a positive integer
    invalid_ts = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM streams
        WHERE abs_unix_ts <= 0
        """
    ).fetchone()["cnt"] or 0
    if invalid_ts:
        issues.append(f"{invalid_ts} streams row(s) have non-positive abs_unix_ts")

    # point_index must be >= 0
    invalid_point_index = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM streams
        WHERE point_index < 0
        """
    ).fetchone()["cnt"] or 0
    if invalid_point_index:
        issues.append(f"{invalid_point_index} streams row(s) have negative point_index")

    return {
        "orphaned_activities": orphaned_activities,
        "orphaned_streams": orphaned_streams,
        "invalid_fk": invalid_fk,
        "null_violations": null_violations,
        "issues": issues,
    }
