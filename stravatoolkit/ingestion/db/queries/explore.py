"""Explore-related database queries."""
from __future__ import annotations

import sqlite3

from ingestion.config import now_utc_iso


def list_explore_stubs(conn: sqlite3.Connection) -> list[dict]:
    """Return athletes discovered by explore/spider that haven't been promoted to tracked yet."""
    rows = conn.execute(
        """
        SELECT athlete_id, name, first_seen_source, first_seen_at, last_seen_at
        FROM athletes
        WHERE is_tracked = 0
          AND first_seen_source IN ('explore', 'spider')
        ORDER BY last_seen_at DESC, athlete_id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def promote_explore_athletes(conn: sqlite3.Connection, athlete_ids: list[int]) -> int:
    """Set is_tracked=1 for the given athlete IDs. Returns count promoted."""
    if not athlete_ids:
        return 0
    placeholders = ",".join("?" * len(athlete_ids))
    cursor = conn.execute(
        f"UPDATE athletes SET is_tracked = 1, backfill_status = 'pending' WHERE athlete_id IN ({placeholders})",
        athlete_ids,
    )
    return int(cursor.rowcount or 0)


def save_explore_segment(conn: sqlite3.Connection, segment_id: int, sport_type: str | None = None) -> None:
    """Persist a discovered segment ID for future leaderboard scraping."""
    conn.execute(
        """
        INSERT INTO explore_segments (segment_id, sport_type, first_seen_at)
        VALUES (?, ?, ?)
        ON CONFLICT(segment_id) DO NOTHING
        """,
        (segment_id, sport_type, now_utc_iso()),
    )


def list_explore_segments(conn: sqlite3.Connection, limit: int = 10) -> list[int]:
    """Return real segment IDs for leaderboard scraping, newest first."""
    rows = conn.execute(
        "SELECT segment_id FROM explore_segments ORDER BY first_seen_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [int(row["segment_id"]) for row in rows]
