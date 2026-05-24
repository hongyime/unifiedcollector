"""Playback-related database queries."""
from __future__ import annotations

import sqlite3

from ingestion.config import day_bounds
from ingestion.db.colors import athlete_color


def _point_or_none(lon: float | None, lat: float | None) -> list[float] | None:
    if lon is None or lat is None:
        return None
    return [lon, lat]


def list_available_dates(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT calendar_date FROM activities ORDER BY calendar_date DESC"
    ).fetchall()
    return [str(row["calendar_date"]) for row in rows]


def build_day_playback(conn: sqlite3.Connection, date_string: str) -> dict:
    day_start, day_end = day_bounds(date_string)
    rows = conn.execute(
        """
        SELECT
            a.activity_id,
            a.athlete_id,
            at.name AS athlete_name,
            a.activity_name,
            a.sport_type,
            at.avatar_url,
            a.privacy_zone_start,
            a.privacy_zone_end,
            a.truncation_point_start_lon,
            a.truncation_point_start_lat,
            a.truncation_point_end_lon,
            a.truncation_point_end_lat,
            a.stream_status,
            MIN(s.abs_unix_ts) AS start_unix,
            MAX(s.abs_unix_ts) AS end_unix
        FROM activities a
        JOIN athletes at ON at.athlete_id = a.athlete_id
        LEFT JOIN streams s ON s.activity_id = a.activity_id
        WHERE a.calendar_date = ?
        GROUP BY a.activity_id
        ORDER BY start_unix ASC, a.activity_id ASC
        """,
        (date_string,),
    ).fetchall()

    trips = []
    athlete_ids = set()
    for row in rows:
        points = conn.execute(
            """
            SELECT longitude, latitude, abs_unix_ts
            FROM streams
            WHERE activity_id = ?
            ORDER BY point_index ASC
            """,
            (row["activity_id"],),
        ).fetchall()
        athlete_ids.add(int(row["athlete_id"]))
        trips.append(
            {
                "activity_id": int(row["activity_id"]),
                "athlete_id": int(row["athlete_id"]),
                "athlete_name": row["athlete_name"],
                "activity_name": row["activity_name"],
                "sport_type": row["sport_type"],
                "color": athlete_color(int(row["athlete_id"])),
                "athlete_avatar_url": row["avatar_url"],
                "start_unix": row["start_unix"],
                "end_unix": row["end_unix"],
                "privacy_zone_start": bool(row["privacy_zone_start"]),
                "privacy_zone_end": bool(row["privacy_zone_end"]),
                "truncation_point_start": _point_or_none(
                    row["truncation_point_start_lon"], row["truncation_point_start_lat"]
                ),
                "truncation_point_end": _point_or_none(
                    row["truncation_point_end_lon"], row["truncation_point_end_lat"]
                ),
                "stream_status": row["stream_status"],
                "path": [[point["longitude"], point["latitude"], point["abs_unix_ts"]] for point in points],
            }
        )

    return {
        "date": date_string,
        "timezone": "Asia/Singapore",
        "day_start_unix": day_start,
        "day_end_unix": day_end,
        "athlete_count": len(athlete_ids),
        "trips": trips,
    }
