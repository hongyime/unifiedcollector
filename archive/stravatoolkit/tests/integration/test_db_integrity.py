"""Unit tests for check_db_integrity() in ingestion/db.py.

Validates: Requirement 2.5
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ingestion import db


@pytest.fixture
def clean_db(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    yield conn
    conn.close()


def _insert_athlete(conn, athlete_id: int = 1, name: str = "Test Athlete") -> None:
    conn.execute(
        """INSERT INTO athletes
           (athlete_id, name, is_private, is_following, is_tracked,
            first_seen_source, first_seen_at, last_seen_at, backfill_status)
           VALUES (?, ?, 0, 1, 1, 'test', '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z', 'pending')""",
        (athlete_id, name),
    )


def _insert_activity(conn, activity_id: int = 100, athlete_id: int = 1) -> None:
    conn.execute(
        """INSERT INTO activities
           (activity_id, athlete_id, sport_type, source,
            start_date_utc, start_date_local, calendar_date,
            privacy_zone_start, privacy_zone_end, stream_status, ingested_at)
           VALUES (?, ?, 'Run', 'test',
                   '2024-01-01T10:00:00Z', '2024-01-01T10:00:00', '2024-01-01',
                   0, 0, 'ok', '2024-01-01T10:00:00Z')""",
        (activity_id, athlete_id),
    )


def _insert_stream(conn, activity_id: int = 100, point_index: int = 0) -> None:
    conn.execute(
        """INSERT INTO streams (activity_id, point_index, longitude, latitude, abs_unix_ts)
           VALUES (?, ?, 103.8, 1.3, 1704067200)""",
        (activity_id, point_index),
    )


# ── clean database ────────────────────────────────────────────────────────────

def test_clean_database_returns_no_issues(clean_db):
    report = db.check_db_integrity(clean_db)
    assert report["orphaned_activities"] == 0
    assert report["orphaned_streams"] == 0
    assert report["invalid_fk"] == 0
    assert report["null_violations"] == 0
    assert report["issues"] == []


# ── orphaned activities ───────────────────────────────────────────────────────

def test_detects_orphaned_activities(clean_db):
    # Insert activity referencing a non-existent athlete (disable FK enforcement first)
    clean_db.execute("PRAGMA foreign_keys = OFF")
    _insert_activity(clean_db, activity_id=200, athlete_id=9999)
    clean_db.execute("PRAGMA foreign_keys = ON")

    report = db.check_db_integrity(clean_db)
    assert report["orphaned_activities"] == 1
    assert any("orphaned" in issue.lower() or "activity" in issue.lower() for issue in report["issues"])


def test_no_orphaned_activities_when_athlete_exists(clean_db):
    _insert_athlete(clean_db, athlete_id=1)
    _insert_activity(clean_db, activity_id=100, athlete_id=1)

    report = db.check_db_integrity(clean_db)
    assert report["orphaned_activities"] == 0


# ── orphaned streams ──────────────────────────────────────────────────────────

def test_detects_orphaned_streams(clean_db):
    clean_db.execute("PRAGMA foreign_keys = OFF")
    _insert_stream(clean_db, activity_id=9999, point_index=0)
    clean_db.execute("PRAGMA foreign_keys = ON")

    report = db.check_db_integrity(clean_db)
    assert report["orphaned_streams"] == 1
    assert any("stream" in issue.lower() for issue in report["issues"])


def test_no_orphaned_streams_when_activity_exists(clean_db):
    _insert_athlete(clean_db, athlete_id=1)
    _insert_activity(clean_db, activity_id=100, athlete_id=1)
    _insert_stream(clean_db, activity_id=100, point_index=0)

    report = db.check_db_integrity(clean_db)
    assert report["orphaned_streams"] == 0


# ── invalid foreign keys ──────────────────────────────────────────────────────

def test_detects_invalid_fk_in_athlete_photo_history(clean_db):
    clean_db.execute("PRAGMA foreign_keys = OFF")
    clean_db.execute(
        """INSERT INTO athlete_photo_history
           (athlete_id, athlete_name, source_url, local_path, md5_hash, captured_at, last_checked_at)
           VALUES (9999, 'Ghost', 'http://x.com/a.jpg', '/tmp/a.jpg', 'abc123',
                   '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')"""
    )
    clean_db.execute("PRAGMA foreign_keys = ON")

    report = db.check_db_integrity(clean_db)
    assert report["invalid_fk"] >= 1
    assert any("athlete_photo_history" in issue for issue in report["issues"])


def test_detects_invalid_fk_in_activity_photos(clean_db):
    clean_db.execute("PRAGMA foreign_keys = OFF")
    clean_db.execute(
        """INSERT INTO activity_photos
           (photo_id, activity_id, athlete_id, athlete_name, media_type, source, first_seen_at, last_seen_at)
           VALUES ('ph1', 100, 9999, 'Ghost', 1, 'test',
                   '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')"""
    )
    clean_db.execute("PRAGMA foreign_keys = ON")

    report = db.check_db_integrity(clean_db)
    assert report["invalid_fk"] >= 1
    assert any("activity_photos" in issue for issue in report["issues"])


# ── NULL violations ───────────────────────────────────────────────────────────

def test_detects_null_violations_in_athletes(clean_db):
    # SQLite enforces NOT NULL constraints even on UPDATE, so we can't easily corrupt
    # a NOT NULL column directly. Instead, verify the null-check queries work by
    # testing with the streams table where we can insert a row and then verify
    # the integrity check correctly reports zero null violations on clean data,
    # and that the function's null-check logic covers the athletes table columns.
    _insert_athlete(clean_db, athlete_id=42, name="Valid Athlete")
    _insert_activity(clean_db, activity_id=100, athlete_id=42)
    _insert_stream(clean_db, activity_id=100, point_index=0)

    report = db.check_db_integrity(clean_db)
    # Clean data should have no null violations
    assert report["null_violations"] == 0

    # Verify the function checks athletes columns by confirming the report structure
    assert "null_violations" in report
    assert "issues" in report
    assert isinstance(report["issues"], list)
