"""Activity-related database queries."""
from __future__ import annotations

import re
import sqlite3

from ingestion.config import now_utc_iso
from ingestion.db.connection import transaction
from ingestion.db.queries.athletes import upsert_athlete


def local_calendar_date_iso(start_date_local: str) -> str:
    if "T" in start_date_local:
        return start_date_local.split("T", 1)[0]
    return start_date_local


def activity_exists_with_terminal_stream(conn: sqlite3.Connection, activity_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM activities
        WHERE activity_id = ?
          AND stream_status IN ('ok', 'incomplete', 'forbidden', 'truncated_empty')
        """,
        (activity_id,),
    ).fetchone()
    return row is not None


def save_activity(
    conn: sqlite3.Connection,
    activity: dict,
    transformed: dict,
    streams_raw: str | None = None,
) -> None:
    start_latlng = activity.get("start_latlng") or [None, None]
    end_latlng = activity.get("end_latlng") or [None, None]
    trunc_start = transformed.get("truncation_point_start") or [None, None]
    trunc_end = transformed.get("truncation_point_end") or [None, None]
    
    # Validate calendar_date format (YYYY-MM-DD)
    calendar_date = local_calendar_date_iso(activity["start_date_local"])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", calendar_date):
        raise ValueError(f"Invalid calendar_date format: {calendar_date}")

    with transaction(conn):
        upsert_athlete(
            conn,
            athlete_id=int(activity["athlete_id"]),
            name=activity["athlete_name"],
            avatar_url=activity.get("athlete_profile_image_url"),
            is_private=bool(activity.get("is_private", False)),
            source=activity.get("source", "following_feed"),
            is_following=bool(activity.get("is_following", False)),
        )
        conn.execute(
            """
            INSERT INTO activities (
                activity_id, athlete_id, activity_name, sport_type, source,
                start_date_utc, start_date_local, calendar_date, elapsed_time_secs,
                start_latlng_lat, start_latlng_lon, end_latlng_lat, end_latlng_lon,
                privacy_zone_start, privacy_zone_end,
                truncation_point_start_lon, truncation_point_start_lat,
                truncation_point_end_lon, truncation_point_end_lat,
                stream_status, streams_raw, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(activity_id) DO UPDATE SET
                athlete_id = excluded.athlete_id,
                activity_name = excluded.activity_name,
                sport_type = excluded.sport_type,
                source = excluded.source,
                start_date_utc = excluded.start_date_utc,
                start_date_local = excluded.start_date_local,
                calendar_date = excluded.calendar_date,
                elapsed_time_secs = excluded.elapsed_time_secs,
                start_latlng_lat = excluded.start_latlng_lat,
                start_latlng_lon = excluded.start_latlng_lon,
                end_latlng_lat = excluded.end_latlng_lat,
                end_latlng_lon = excluded.end_latlng_lon,
                privacy_zone_start = excluded.privacy_zone_start,
                privacy_zone_end = excluded.privacy_zone_end,
                truncation_point_start_lon = excluded.truncation_point_start_lon,
                truncation_point_start_lat = excluded.truncation_point_start_lat,
                truncation_point_end_lon = excluded.truncation_point_end_lon,
                truncation_point_end_lat = excluded.truncation_point_end_lat,
                stream_status = excluded.stream_status,
                streams_raw = excluded.streams_raw,
                ingested_at = excluded.ingested_at
            """,
            (
                int(activity["activity_id"]),
                int(activity["athlete_id"]),
                activity.get("activity_name"),
                activity.get("sport_type", "Unknown"),
                activity.get("source", "following_feed"),
                activity["start_date_utc"],
                activity["start_date_local"],
                local_calendar_date_iso(activity["start_date_local"]),
                activity.get("elapsed_time", 0),
                start_latlng[0],
                start_latlng[1],
                end_latlng[0],
                end_latlng[1],
                int(bool(transformed["privacy_zone_start"])),
                int(bool(transformed["privacy_zone_end"])),
                trunc_start[0],
                trunc_start[1],
                trunc_end[0],
                trunc_end[1],
                transformed["stream_status"],
                streams_raw,
                now_utc_iso(),
            ),
        )
        conn.execute("DELETE FROM streams WHERE activity_id = ?", (int(activity["activity_id"]),))
        for index, point in enumerate(transformed["path"]):
            conn.execute(
                """
                INSERT INTO streams (activity_id, point_index, longitude, latitude, abs_unix_ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (int(activity["activity_id"]), index, point[0], point[1], point[2]),
            )


def save_activity_photos(conn: sqlite3.Connection, activity: dict) -> int:
    photos = activity.get("activity_photos") or []
    if not photos:
        return 0

    athlete_id = int(activity["athlete_id"])
    upsert_athlete(
        conn,
        athlete_id=athlete_id,
        name=activity["athlete_name"],
        avatar_url=activity.get("athlete_profile_image_url"),
        is_private=bool(activity.get("is_private", False)),
        source=activity.get("source", "following_feed"),
        is_following=bool(activity.get("is_following", False)),
    )

    saved = 0
    now = now_utc_iso()
    calendar_date = local_calendar_date_iso(activity["start_date_local"])
    with transaction(conn):
        for photo in photos:
            photo_id = str(photo.get("photo_id") or "").strip()
            if not photo_id:
                continue
            conn.execute(
                """
                INSERT INTO activity_photos (
                    photo_id, activity_id, athlete_id, athlete_name, activity_name,
                    calendar_date, start_date_utc, caption, media_type, source,
                    source_url_large, source_url_thumbnail, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(photo_id) DO UPDATE SET
                    activity_id = excluded.activity_id,
                    athlete_id = excluded.athlete_id,
                    athlete_name = excluded.athlete_name,
                    activity_name = COALESCE(excluded.activity_name, activity_photos.activity_name),
                    calendar_date = COALESCE(excluded.calendar_date, activity_photos.calendar_date),
                    start_date_utc = COALESCE(excluded.start_date_utc, activity_photos.start_date_utc),
                    caption = COALESCE(excluded.caption, activity_photos.caption),
                    media_type = excluded.media_type,
                    source = excluded.source,
                    source_url_large = COALESCE(excluded.source_url_large, activity_photos.source_url_large),
                    source_url_thumbnail = COALESCE(excluded.source_url_thumbnail, activity_photos.source_url_thumbnail),
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    photo_id,
                    int(photo.get("activity_id") or activity["activity_id"]),
                    int(photo.get("athlete_id") or athlete_id),
                    photo.get("athlete_name") or activity["athlete_name"],
                    photo.get("activity_name") or activity.get("activity_name"),
                    local_calendar_date_iso(photo.get("start_date_local") or activity["start_date_local"]),
                    photo.get("start_date_utc") or activity["start_date_utc"],
                    photo.get("caption"),
                    int(photo.get("media_type") or 1),
                    photo.get("source") or activity.get("source", "following_feed"),
                    photo.get("source_url_large"),
                    photo.get("source_url_thumbnail"),
                    now,
                    now,
                ),
            )
            saved += 1
    return saved


def reset_activity_stream_status(
    conn: sqlite3.Connection,
    athlete_id: int | None = None,
) -> int:
    """Reset stream_status to 'pending' for activities so they will be re-scraped.

    Clears streams_raw and deletes existing stream points for the affected activities.

    Args:
        conn: Database connection.
        athlete_id: If provided, only reset activities for this athlete.
                    If None, reset activities for all athletes.

    Returns:
        Number of activities reset.
    """
    if athlete_id is not None:
        activity_ids = [
            row["activity_id"]
            for row in conn.execute(
                "SELECT activity_id FROM activities WHERE athlete_id = ?",
                (athlete_id,),
            ).fetchall()
        ]
    else:
        activity_ids = [
            row["activity_id"]
            for row in conn.execute("SELECT activity_id FROM activities").fetchall()
        ]

    if not activity_ids:
        return 0

    placeholders = ",".join("?" * len(activity_ids))

    with transaction(conn):
        conn.execute(
            f"DELETE FROM streams WHERE activity_id IN ({placeholders})",
            activity_ids,
        )
        conn.execute(
            f"""UPDATE activities
                SET stream_status = 'pending', streams_raw = NULL
                WHERE activity_id IN ({placeholders})""",
            activity_ids,
        )

    return len(activity_ids)


def list_activity_photo_targets(conn: sqlite3.Connection, date_string: str | None = None) -> list[dict]:
    query = """
        SELECT
            ap.photo_id,
            ap.activity_id,
            ap.athlete_id,
            ap.athlete_name,
            ap.activity_name,
            ap.calendar_date,
            act.start_date_local,
            ap.start_date_utc,
            ap.caption,
            ap.media_type,
            ap.source,
            ap.source_url_large,
            ap.source_url_thumbnail,
            ap.local_path,
            ap.md5_hash,
            ap.downloaded_at
        FROM activity_photos ap
        JOIN athletes at ON at.athlete_id = ap.athlete_id
        LEFT JOIN activities act ON act.activity_id = ap.activity_id
        WHERE at.is_tracked = 1
    """
    params: list[str] = []
    if date_string:
        query += " AND ap.calendar_date = ?"
        params.append(date_string)
    query += " ORDER BY ap.calendar_date DESC, ap.athlete_name ASC, ap.activity_id DESC, ap.photo_id ASC"
    rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def mark_activity_photo_downloaded(conn: sqlite3.Connection, photo_id: str, local_path: str, md5_hash: str) -> None:
    conn.execute(
        """
        UPDATE activity_photos
        SET local_path = ?,
            md5_hash = ?,
            downloaded_at = ?
        WHERE photo_id = ?
        """,
        (local_path, md5_hash, now_utc_iso(), photo_id),
    )
