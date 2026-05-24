from __future__ import annotations

from pathlib import Path

from ingestion import db


def test_following_roster_sync_and_backfill_status(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        count = db.sync_following_roster(
            conn,
            [
                {"athlete_id": 1, "name": "Alice", "avatar_url": None, "source": "following_roster"},
                {"athlete_id": 2, "name": "Bob", "avatar_url": None, "source": "following_roster"},
            ],
        )
        assert count == 2

        candidates = db.get_following_backfill_candidates(conn)
        assert [row["athlete_id"] for row in candidates] == [1, 2]

        db.update_backfill_progress(
            conn,
            1,
            cursor_before="2026-01-01T00:00:00Z",
            oldest_seen_utc="2025-12-31T00:00:00Z",
            status="active",
        )
        status = db.get_status_summary(conn)
        assert status["follow_roster_size"] == 2
        assert status["backfill_pending"] == 2
        assert status["backfill_degraded"] == 0

        db.update_backfill_progress(
            conn,
            2,
            cursor_before="2026-02-01T00:00:00Z",
            oldest_seen_utc=None,
            status="degraded",
            issue_code="parse_empty",
            issue_message="profile page loaded but no usable activities could be parsed",
            issue_at="2026-04-11T08:30:00+00:00",
        )
        degraded = conn.execute(
            """
            SELECT backfill_status, backfill_last_issue_code, backfill_last_issue_message, backfill_last_issue_at
            FROM athletes
            WHERE athlete_id = 2
            """
        ).fetchone()
        status = db.get_status_summary(conn)
        assert degraded["backfill_status"] == "degraded"
        assert degraded["backfill_last_issue_code"] == "parse_empty"
        assert degraded["backfill_last_issue_message"] == "profile page loaded but no usable activities could be parsed"
        assert degraded["backfill_last_issue_at"] == "2026-04-11T08:30:00+00:00"
        assert status["backfill_pending"] == 2
        assert status["backfill_degraded"] == 1

        db.update_backfill_progress(
            conn,
            1,
            cursor_before="2025-12-31T00:00:00Z",
            oldest_seen_utc="2025-12-01T00:00:00Z",
            status="complete",
            completed=True,
        )
        status = db.get_status_summary(conn)
        assert status["backfill_completed"] == 1
        assert status["backfill_pending"] == 1

        db.update_backfill_progress(
            conn,
            2,
            cursor_before="2025-11-01T00:00:00Z",
            oldest_seen_utc="2025-10-01T00:00:00Z",
            status="active",
        )
        cleared = conn.execute(
            """
            SELECT backfill_last_issue_code, backfill_last_issue_message, backfill_last_issue_at
            FROM athletes
            WHERE athlete_id = 2
            """
        ).fetchone()
        status = db.get_status_summary(conn)
        assert cleared["backfill_last_issue_code"] is None
        assert cleared["backfill_last_issue_message"] is None
        assert cleared["backfill_last_issue_at"] is None
        assert status["backfill_degraded"] == 0
    finally:
        conn.close()


def test_roster_refresh_keeps_unfollowed_athletes_tracked(tmp_path: Path) -> None:
    db_path = tmp_path / "tracked.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        db.sync_following_roster(
            conn,
            [
                {"athlete_id": 1, "name": "Alice", "avatar_url": None, "source": "following_roster"},
                {"athlete_id": 2, "name": "Bob", "avatar_url": None, "source": "following_roster"},
            ],
        )
        db.sync_following_roster(
            conn,
            [{"athlete_id": 1, "name": "Alice", "avatar_url": None, "source": "following_roster"}],
        )

        rows = conn.execute(
            "SELECT athlete_id, is_following, is_tracked FROM athletes ORDER BY athlete_id"
        ).fetchall()
        candidates = db.get_following_backfill_candidates(conn)
        status = db.get_status_summary(conn)

        assert [(row["athlete_id"], row["is_following"], row["is_tracked"]) for row in rows] == [
            (1, 1, 1),
            (2, 0, 1),
        ]
        assert [row["athlete_id"] for row in candidates] == [1, 2]
        assert status["follow_roster_size"] == 1
        assert status["tracked_roster_size"] == 2
    finally:
        conn.close()


def test_upsert_self_athlete_is_tracked_but_not_following(tmp_path: Path) -> None:
    db_path = tmp_path / "self.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        db.upsert_athlete(
            conn,
            athlete_id=7,
            name="Bryan",
            source="self",
            is_following=False,
            is_tracked=True,
        )

        athlete = conn.execute(
            "SELECT athlete_id, is_following, is_tracked, first_seen_source FROM athletes WHERE athlete_id = 7"
        ).fetchone()
        candidates = db.get_following_backfill_candidates(conn)

        assert athlete is not None
        assert athlete["is_following"] == 0
        assert athlete["is_tracked"] == 1
        assert athlete["first_seen_source"] == "self"
        assert [row["athlete_id"] for row in candidates] == [7]
    finally:
        conn.close()


def test_save_activity_builds_playback_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        activity = {
            "activity_id": 101,
            "athlete_id": 5,
            "athlete_name": "Casey",
            "activity_name": "Lunch Ride",
            "sport_type": "Ride",
            "start_date_utc": "2026-04-06T04:00:00Z",
            "start_date_local": "2026-04-06T12:00:00+08:00",
            "elapsed_time": 60,
            "start_latlng": [1.3, 103.8],
            "end_latlng": [1.31, 103.81],
            "source": "following_feed",
            "is_following": True,
        }
        transformed = {
            "stream_status": "ok",
            "privacy_zone_start": False,
            "privacy_zone_end": False,
            "truncation_point_start": None,
            "truncation_point_end": None,
            "path": [[103.8, 1.3, 1712376000], [103.81, 1.31, 1712376060]],
        }
        db.save_activity(conn, activity, transformed)

        payload = db.build_day_playback(conn, "2026-04-06")
        assert payload["athlete_count"] == 1
        assert payload["trips"][0]["activity_id"] == 101
        assert len(payload["trips"][0]["path"]) == 2
        assert db.activity_exists_with_terminal_stream(conn, 101) is True
    finally:
        conn.close()


def test_save_activity_photos_tracks_photo_only_entries(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        activity = {
            "activity_id": 202,
            "athlete_id": 9,
            "athlete_name": "Photo Person",
            "athlete_profile_image_url": "https://example.com/avatar.jpg",
            "activity_name": "Photo Run",
            "sport_type": "Run",
            "start_date_utc": "2026-04-06T04:00:00Z",
            "start_date_local": "2026-04-06T12:00:00+08:00",
            "source": "following_feed",
            "is_following": True,
            "activity_photos": [
                {
                    "photo_id": "photo-123",
                    "activity_id": 202,
                    "athlete_id": 9,
                    "athlete_name": "Photo Person",
                    "activity_name": "Photo Run",
                    "source_url_large": "https://example.com/photo-large.jpg",
                    "source_url_thumbnail": "https://example.com/photo-thumb.jpg",
                    "start_date_utc": "2026-04-06T04:00:00Z",
                    "start_date_local": "2026-04-06T12:00:00+08:00",
                    "source": "following_feed",
                }
            ],
        }

        saved = db.save_activity_photos(conn, activity)
        targets = db.list_activity_photo_targets(conn, date_string="2026-04-06")

        assert saved == 1
        assert len(targets) == 1
        assert targets[0]["photo_id"] == "photo-123"
        assert targets[0]["athlete_name"] == "Photo Person"
    finally:
        conn.close()
