"""B5: DB recording for browser downloads.

Reads/writes media_items and operation_progress tables.
Two-level dedup:
  - Username level: operation_progress(download, username=completed) → skip all
  - Post level:     media_items(shortcode, status=completed)          → skip post
"""
from __future__ import annotations

import time
from datetime import datetime, timezone


def username_completed(db, username: str) -> bool:
    """True if all posts for username were previously downloaded."""
    row = db.fetchone(
        "SELECT status FROM operation_progress WHERE operation_id='download' AND username=?",
        (username,),
    )
    return row is not None and row["status"] == "completed"


def shortcode_completed(db, shortcode: str) -> bool:
    """True if this specific post shortcode is already on disk."""
    row = db.fetchone(
        "SELECT id FROM media_items WHERE shortcode=? AND download_status='completed'",
        (shortcode,),
    )
    return row is not None


def record_media_item(db, post_data: dict, file_info: dict) -> None:
    """Upsert one downloaded media item into media_items."""
    taken_at_ts = post_data["taken_at"].timestamp() if isinstance(post_data["taken_at"], datetime) else 0.0
    db.execute(
        """INSERT INTO media_items
               (username, shortcode, media_type, file_path, file_hash, file_size,
                taken_at, downloaded_at, download_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed')
           ON CONFLICT(shortcode) DO UPDATE SET
               file_path=excluded.file_path,
               file_hash=excluded.file_hash,
               file_size=excluded.file_size,
               downloaded_at=excluded.downloaded_at,
               download_status='completed'
        """,
        (
            post_data["username"],
            post_data["shortcode"],
            file_info["media_type"],
            file_info["file_path"],
            file_info["file_hash"],
            file_info["file_size"],
            taken_at_ts,
            time.time(),
        ),
    )


def mark_shortcode_failed(db, shortcode: str, username: str) -> None:
    db.execute(
        """INSERT INTO media_items (username, shortcode, media_type, download_status, downloaded_at)
           VALUES (?, ?, 'unknown', 'failed', ?)
           ON CONFLICT(shortcode) DO UPDATE SET download_status='failed'""",
        (username, shortcode, time.time()),
    )


def mark_username_completed(db, username: str) -> None:
    db.execute(
        """INSERT INTO operation_progress (operation_id, username, status, updated_at)
           VALUES ('download', ?, 'completed', ?)
           ON CONFLICT(operation_id, username) DO UPDATE SET
               status='completed', updated_at=excluded.updated_at""",
        (username, time.time()),
    )


def mark_username_failed(db, username: str, error: str = "") -> None:
    db.execute(
        """INSERT INTO operation_progress (operation_id, username, status, error_msg, updated_at)
           VALUES ('download', ?, 'failed', ?, ?)
           ON CONFLICT(operation_id, username) DO UPDATE SET
               status='failed', error_msg=excluded.error_msg, updated_at=excluded.updated_at""",
        (username, error[:500], time.time()),
    )
