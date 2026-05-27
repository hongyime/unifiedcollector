"""Athlete-related database queries."""
from __future__ import annotations

import sqlite3
from typing import Iterable

from ingestion.config import now_utc_iso
from ingestion.db.connection import transaction
from ingestion.db.colors import athlete_color


def _safe_str(value: str) -> str:
    return value.encode("utf-8", errors="replace").decode("utf-8")


def upsert_athlete(
    conn: sqlite3.Connection,
    athlete_id: int,
    name: str,
    avatar_url: str | None = None,
    is_private: bool = False,
    source: str = "unknown",
    is_following: bool = False,
    is_tracked: bool = True,
    roster_refreshed_at: str | None = None,
) -> None:
    now = now_utc_iso()
    name = _safe_str(name)
    conn.execute(
        """
        INSERT INTO athletes (
            athlete_id, name, avatar_url, is_private, is_following, is_tracked,
            first_seen_source, first_seen_at, last_seen_at, roster_refreshed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(athlete_id) DO UPDATE SET
            name = excluded.name,
            avatar_url = COALESCE(excluded.avatar_url, athletes.avatar_url),
            is_private = excluded.is_private,
            is_following = excluded.is_following,
            is_tracked = CASE
                WHEN excluded.is_tracked = 1 THEN 1
                ELSE athletes.is_tracked
            END,
            last_seen_at = excluded.last_seen_at,
            roster_refreshed_at = COALESCE(excluded.roster_refreshed_at, athletes.roster_refreshed_at)
        """,
        (
            athlete_id,
            name,
            avatar_url,
            int(is_private),
            int(is_following),
            int(is_tracked),
            source,
            now,
            now,
            roster_refreshed_at,
        ),
    )


def sync_following_roster(conn: sqlite3.Connection, athletes: Iterable[dict]) -> int:
    refreshed_at = now_utc_iso()
    athlete_ids = [int(athlete["athlete_id"]) for athlete in athletes]
    with transaction(conn):
        conn.execute("UPDATE athletes SET is_following = 0 WHERE is_following = 1")
        for athlete in athletes:
            upsert_athlete(
                conn,
                athlete_id=int(athlete["athlete_id"]),
                name=athlete["name"],
                avatar_url=athlete.get("avatar_url"),
                is_private=bool(athlete.get("is_private", False)),
                source=athlete.get("source", "following_roster"),
                is_following=True,
                is_tracked=True,
                roster_refreshed_at=refreshed_at,
            )
    return len(athlete_ids)


def list_athletes(
    conn: sqlite3.Connection,
    date_string: str | None = None,
    month_string: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    params: list[str] = []
    query = """
        SELECT
            at.athlete_id,
            at.name,
            at.avatar_url,
            at.is_following,
            at.is_tracked,
            at.backfill_status,
            at.backfill_completed_at,
            at.backfill_oldest_seen_utc,
            at.backfill_last_issue_code,
            at.backfill_last_issue_message,
            at.backfill_last_issue_at,
            COUNT(a.activity_id) AS activity_count
        FROM athletes at
        LEFT JOIN activities a ON a.athlete_id = at.athlete_id
    """
    if date_string:
        query += " AND a.calendar_date = ?"
        params.append(date_string)
    elif month_string:
        query += " AND substr(a.calendar_date, 1, 7) = ?"
        params.append(month_string)
    query += """
        GROUP BY at.athlete_id
    """
    if date_string or month_string:
        query += "\n HAVING COUNT(a.activity_id) > 0"
    query += """
        ORDER BY activity_count DESC, at.is_following DESC, at.name ASC
        LIMIT ? OFFSET ?
    """
    params.extend([str(limit), str(offset)])

    rows = conn.execute(query, tuple(params)).fetchall()
    return [
        {
            "athlete_id": int(row["athlete_id"]),
            "name": row["name"],
            "avatar_url": row["avatar_url"],
            "is_following": bool(row["is_following"]),
            "is_tracked": bool(row["is_tracked"]),
            "activity_count": int(row["activity_count"] or 0),
            "backfill_status": row["backfill_status"] or "pending",
            "backfill_completed_at": row["backfill_completed_at"],
            "backfill_oldest_seen_utc": row["backfill_oldest_seen_utc"],
            "backfill_last_issue_code": row["backfill_last_issue_code"],
            "backfill_last_issue_message": row["backfill_last_issue_message"],
            "backfill_last_issue_at": row["backfill_last_issue_at"],
            "color": athlete_color(int(row["athlete_id"])),
        }
        for row in rows
    ]


def get_athlete_detail(conn: sqlite3.Connection, athlete_id: int, month_string: str | None = None) -> dict | None:
    params: list[str | int] = []
    activity_join = "LEFT JOIN activities a ON a.athlete_id = at.athlete_id"
    if month_string:
        activity_join += " AND substr(a.calendar_date, 1, 7) = ?"
        params.append(month_string)
    params.append(athlete_id)
    athlete = conn.execute(
        """
        SELECT
            at.athlete_id,
            at.name,
            at.avatar_url,
            at.is_following,
            at.is_tracked,
            at.backfill_status,
            at.backfill_completed_at,
            at.backfill_oldest_seen_utc,
            at.backfill_last_issue_code,
            at.backfill_last_issue_message,
            at.backfill_last_issue_at,
            COUNT(a.activity_id) AS activity_count
        FROM athletes at
        """
        + activity_join
        + """
        WHERE at.athlete_id = ?
        GROUP BY at.athlete_id
        """,
        tuple(params),
    ).fetchone()
    if athlete is None:
        return None

    recent_query = """
        SELECT
            activity_id,
            activity_name,
            sport_type,
            calendar_date,
            start_date_utc,
            source,
            stream_status
        FROM activities
        WHERE athlete_id = ?
    """
    recent_params: list[str | int] = [athlete_id]
    if month_string:
        recent_query += " AND substr(calendar_date, 1, 7) = ?"
        recent_params.append(month_string)
    recent_query += """
        ORDER BY start_date_utc DESC
        LIMIT 8
    """
    recent_rows = conn.execute(
        recent_query,
        tuple(recent_params),
    ).fetchall()

    return {
        "athlete_id": int(athlete["athlete_id"]),
        "name": athlete["name"],
        "avatar_url": athlete["avatar_url"],
        "is_following": bool(athlete["is_following"]),
        "is_tracked": bool(athlete["is_tracked"]),
        "activity_count": int(athlete["activity_count"] or 0),
        "backfill_status": athlete["backfill_status"] or "pending",
        "backfill_completed_at": athlete["backfill_completed_at"],
        "backfill_oldest_seen_utc": athlete["backfill_oldest_seen_utc"],
        "backfill_last_issue_code": athlete["backfill_last_issue_code"],
        "backfill_last_issue_message": athlete["backfill_last_issue_message"],
        "backfill_last_issue_at": athlete["backfill_last_issue_at"],
        "color": athlete_color(int(athlete["athlete_id"])),
        "recent_activities": [
            {
                "activity_id": int(row["activity_id"]),
                "activity_name": row["activity_name"],
                "sport_type": row["sport_type"],
                "calendar_date": row["calendar_date"],
                "start_date_utc": row["start_date_utc"],
                "source": row["source"],
                "stream_status": row["stream_status"],
            }
            for row in recent_rows
        ],
    }


def build_athlete_route_history(conn: sqlite3.Connection, athlete_id: int) -> dict | None:
    from ingestion.db.colors import activity_palette
    
    athlete = conn.execute(
        """
        SELECT athlete_id, name
        FROM athletes
        WHERE athlete_id = ?
        """,
        (athlete_id,),
    ).fetchone()
    if athlete is None:
        return None

    activity_rows = conn.execute(
        """
        SELECT
            activity_id,
            activity_name,
            sport_type,
            calendar_date,
            stream_status
        FROM activities
        WHERE athlete_id = ?
        ORDER BY calendar_date DESC, start_date_utc DESC, activity_id DESC
        """,
        (athlete_id,),
    ).fetchall()

    routes = []
    for index, row in enumerate(activity_rows):
        points = conn.execute(
            """
            SELECT longitude, latitude, abs_unix_ts
            FROM streams
            WHERE activity_id = ?
            ORDER BY point_index ASC
            """,
            (row["activity_id"],),
        ).fetchall()
        routes.append(
            {
                "activity_id": int(row["activity_id"]),
                "activity_name": row["activity_name"],
                "sport_type": row["sport_type"],
                "calendar_date": row["calendar_date"],
                "start_unix": points[0]["abs_unix_ts"] if points else None,
                "end_unix": points[-1]["abs_unix_ts"] if points else None,
                "stream_status": row["stream_status"],
                "color": activity_palette(index),
                "path": [[point["longitude"], point["latitude"], point["abs_unix_ts"]] for point in points],
            }
        )

    return {
        "athlete_id": int(athlete["athlete_id"]),
        "name": athlete["name"],
        "activity_count": len(routes),
        "routes": routes,
    }
