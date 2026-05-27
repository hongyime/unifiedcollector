"""Unit tests for reset_activity_stream_status() in ingestion/db.py.

Validates: Requirement 2.6
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ingestion import db


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    c = db.connect(db_path)
    yield c
    c.close()


def _athlete(c, athlete_id: int = 1) -> None:
    c.execute(
        """INSERT INTO athletes
           (athlete_id, name, is_private, is_following, is_tracked,
            first_seen_source, first_seen_at, last_seen_at, backfill_status)
           VALUES (?, 'Athlete', 0, 1, 1, 'test',
                   '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z', 'pending')""",
        (athlete_id,),
    )


def _activity(c, activity_id: int, athlete_id: int = 1, stream_status: str = "ok") -> None:
    c.execute(
        """INSERT INTO activities
           (activity_id, athlete_id, sport_type, source,
            start_date_utc, start_date_local, calendar_date,
            privacy_zone_start, privacy_zone_end, stream_status,
            streams_raw, ingested_at)
           VALUES (?, ?, 'Run', 'test',
                   '2024-01-01T10:00:00Z', '2024-01-01T10:00:00', '2024-01-01',
                   0, 0, ?, '{"latlng":[]}', '2024-01-01T10:00:00Z')""",
        (activity_id, athlete_id, stream_status),
    )


def _stream(c, activity_id: int, point_index: int = 0) -> None:
    c.execute(
        """INSERT INTO streams (activity_id, point_index, longitude, latitude, abs_unix_ts)
           VALUES (?, ?, 103.8, 1.3, 1704067200)""",
        (activity_id, point_index),
    )


# ── reset for specific athlete ────────────────────────────────────────────────

def test_reset_specific_athlete(conn):
    _athlete(conn, 1)
    _athlete(conn, 2)
    _activity(conn, 101, athlete_id=1)
    _activity(conn, 102, athlete_id=2)

    count = db.reset_activity_stream_status(conn, athlete_id=1)

    assert count == 1
    row = conn.execute("SELECT stream_status FROM activities WHERE activity_id = 101").fetchone()
    assert row["stream_status"] == "pending"
    # athlete 2's activity should be untouched
    row2 = conn.execute("SELECT stream_status FROM activities WHERE activity_id = 102").fetchone()
    assert row2["stream_status"] == "ok"


# ── reset for all athletes ────────────────────────────────────────────────────

def test_reset_all_athletes(conn):
    _athlete(conn, 1)
    _athlete(conn, 2)
    _activity(conn, 101, athlete_id=1)
    _activity(conn, 102, athlete_id=2)

    count = db.reset_activity_stream_status(conn, athlete_id=None)

    assert count == 2
    for aid in (101, 102):
        row = conn.execute("SELECT stream_status FROM activities WHERE activity_id = ?", (aid,)).fetchone()
        assert row["stream_status"] == "pending"


# ── stream_status set to 'pending' ───────────────────────────────────────────

def test_stream_status_set_to_pending(conn):
    _athlete(conn, 1)
    _activity(conn, 101, athlete_id=1, stream_status="ok")

    db.reset_activity_stream_status(conn, athlete_id=1)

    row = conn.execute("SELECT stream_status FROM activities WHERE activity_id = 101").fetchone()
    assert row["stream_status"] == "pending"


# ── streams_raw cleared ───────────────────────────────────────────────────────

def test_streams_raw_cleared(conn):
    _athlete(conn, 1)
    _activity(conn, 101, athlete_id=1)

    # Confirm streams_raw is set before reset
    before = conn.execute("SELECT streams_raw FROM activities WHERE activity_id = 101").fetchone()
    assert before["streams_raw"] is not None

    db.reset_activity_stream_status(conn, athlete_id=1)

    after = conn.execute("SELECT streams_raw FROM activities WHERE activity_id = 101").fetchone()
    assert after["streams_raw"] is None


# ── streams records deleted ───────────────────────────────────────────────────

def test_streams_records_deleted(conn):
    _athlete(conn, 1)
    _activity(conn, 101, athlete_id=1)
    _stream(conn, 101, 0)
    _stream(conn, 101, 1)

    before = conn.execute("SELECT COUNT(*) AS cnt FROM streams WHERE activity_id = 101").fetchone()
    assert before["cnt"] == 2

    db.reset_activity_stream_status(conn, athlete_id=1)

    after = conn.execute("SELECT COUNT(*) AS cnt FROM streams WHERE activity_id = 101").fetchone()
    assert after["cnt"] == 0


# ── return count is correct ───────────────────────────────────────────────────

def test_return_count_correct(conn):
    _athlete(conn, 1)
    for i in range(5):
        _activity(conn, 200 + i, athlete_id=1)

    count = db.reset_activity_stream_status(conn, athlete_id=1)
    assert count == 5


def test_return_count_zero_when_no_activities(conn):
    _athlete(conn, 1)
    count = db.reset_activity_stream_status(conn, athlete_id=1)
    assert count == 0
