"""Photo-related database queries."""
from __future__ import annotations

import sqlite3

from ingestion.config import now_utc_iso


def list_profile_photo_targets(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT athlete_id, name, avatar_url
        FROM athletes
        WHERE is_tracked = 1
          AND avatar_url IS NOT NULL
          AND avatar_url != ''
        ORDER BY name ASC, athlete_id ASC
        """
    ).fetchall()
    return [
        {
            "athlete_id": int(row["athlete_id"]),
            "name": row["name"],
            "avatar_url": row["avatar_url"],
        }
        for row in rows
    ]


def get_latest_profile_photo(conn: sqlite3.Connection, athlete_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, athlete_id, athlete_name, source_url, local_path, md5_hash, captured_at, last_checked_at
        FROM athlete_photo_history
        WHERE athlete_id = ?
        ORDER BY captured_at DESC, id DESC
        LIMIT 1
        """,
        (athlete_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def insert_profile_photo_history(
    conn: sqlite3.Connection,
    athlete_id: int,
    athlete_name: str,
    source_url: str,
    local_path: str,
    md5_hash: str,
) -> None:
    now = now_utc_iso()
    conn.execute(
        """
        INSERT INTO athlete_photo_history (
            athlete_id, athlete_name, source_url, local_path, md5_hash, captured_at, last_checked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (athlete_id, athlete_name, source_url, local_path, md5_hash, now, now),
    )


def touch_profile_photo_history(conn: sqlite3.Connection, record_id: int) -> None:
    conn.execute(
        """
        UPDATE athlete_photo_history
        SET last_checked_at = ?
        WHERE id = ?
        """,
        (now_utc_iso(), record_id),
    )
