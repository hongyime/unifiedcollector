from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from ingestion import db


def test_status_api_reports_backfill_progress(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "api.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
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
        run_id = db.create_crawl_run(conn, "daily_sync", "2026-04-06", roster_refreshed=True, backfill_step_limit=15)
        db.finalize_crawl_run(conn, run_id, "ok")
        db.update_backfill_progress(conn, 1, cursor_before=None, oldest_seen_utc=None, status="complete", completed=True)
        db.update_backfill_progress(
            conn,
            2,
            cursor_before="2026-03-01T00:00:00Z",
            oldest_seen_utc=None,
            status="degraded",
            issue_code="parse_empty",
            issue_message="profile page loaded but no usable activities could be parsed",
            issue_at="2026-04-11T08:30:00+00:00",
        )
    finally:
        conn.close()

    client = TestClient(app)
    response = client.get("/api/v1/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["follow_roster_size"] == 2
    assert payload["tracked_roster_size"] == 2
    assert payload["backfill_completed"] == 1
    assert payload["backfill_pending"] == 1
    assert payload["backfill_degraded"] == 1
    assert payload["backfill_needs_endpoint"] == 0
    assert payload["last_successful_sync_date"] == "2026-04-06"


def test_activities_api_returns_day_playback_payload(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "activities.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        db.sync_following_roster(
            conn,
            [{"athlete_id": 7, "name": "Jordan", "avatar_url": "https://example.com/jordan.jpg", "source": "following_roster"}],
        )
        activity = {
            "activity_id": 501,
            "athlete_id": 7,
            "athlete_name": "Jordan",
            "activity_name": "Lunch Run",
            "sport_type": "Run",
            "start_date_utc": "2026-04-06T04:00:00Z",
            "start_date_local": "2026-04-06T12:00:00+08:00",
            "elapsed_time": 1200,
            "start_latlng": [1.3, 103.8],
            "end_latlng": [1.31, 103.81],
            "source": "following_feed",
            "is_following": True,
            "athlete_profile_image_url": "https://example.com/jordan.jpg",
        }
        transformed = {
            "stream_status": "ok",
            "privacy_zone_start": False,
            "privacy_zone_end": True,
            "truncation_point_start": None,
            "truncation_point_end": [103.81, 1.31],
            "path": [[103.8, 1.3, 1712376000], [103.81, 1.31, 1712376060]],
        }
        db.save_activity(conn, activity, transformed)
    finally:
        conn.close()

    client = TestClient(app)
    response = client.get("/api/v1/activities?date=2026-04-06")
    assert response.status_code == 200
    payload = response.json()
    assert payload["date"] == "2026-04-06"
    assert payload["athlete_count"] == 1
    assert len(payload["trips"]) == 1
    assert payload["trips"][0]["activity_id"] == 501
    assert payload["trips"][0]["athlete_id"] == 7
    assert payload["trips"][0]["path"][0] == [103.8, 1.3, 1712376000]


def test_athletes_api_returns_filtered_roster(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "athletes.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        db.sync_following_roster(
            conn,
            [{"athlete_id": 7, "name": "Jordan", "avatar_url": "https://example.com/jordan.jpg", "source": "following_roster"}],
        )
        activity = {
            "activity_id": 501,
            "athlete_id": 7,
            "athlete_name": "Jordan",
            "activity_name": "Lunch Run",
            "sport_type": "Run",
            "start_date_utc": "2026-04-06T04:00:00Z",
            "start_date_local": "2026-04-06T12:00:00+08:00",
            "elapsed_time": 1200,
            "start_latlng": [1.3, 103.8],
            "end_latlng": [1.31, 103.81],
            "source": "following_feed",
            "is_following": True,
            "athlete_profile_image_url": "https://example.com/jordan.jpg",
        }
        transformed = {
            "stream_status": "ok",
            "privacy_zone_start": False,
            "privacy_zone_end": True,
            "truncation_point_start": None,
            "truncation_point_end": [103.81, 1.31],
            "path": [[103.8, 1.3, 1712376000], [103.81, 1.31, 1712376060]],
        }
        db.save_activity(conn, activity, transformed)
    finally:
        conn.close()

    client = TestClient(app)
    response = client.get("/api/v1/athletes?date=2026-04-06")
    assert response.status_code == 200
    payload = response.json()
    assert payload["athletes"][0]["athlete_id"] == 7
    assert payload["athletes"][0]["name"] == "Jordan"
    assert payload["athletes"][0]["activity_count"] == 1
    assert payload["athletes"][0]["is_tracked"] is True
    assert payload["athletes"][0]["color"] == [234, 83, 226]


def test_athlete_detail_api_returns_recent_activities(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "athlete-detail.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        db.sync_following_roster(
            conn,
            [{"athlete_id": 9, "name": "Riley", "avatar_url": None, "source": "following_roster"}],
        )
        db.update_backfill_progress(
            conn,
            9,
            cursor_before="2026-03-01T00:00:00Z",
            oldest_seen_utc=None,
            status="degraded",
            issue_code="parse_empty",
            issue_message="profile page loaded but no usable activities could be parsed",
            issue_at="2026-04-11T08:30:00+00:00",
        )
        activity = {
            "activity_id": 777,
            "athlete_id": 9,
            "athlete_name": "Riley",
            "activity_name": "Evening Ride",
            "sport_type": "Ride",
            "start_date_utc": "2026-04-05T10:00:00Z",
            "start_date_local": "2026-04-05T18:00:00+08:00",
            "elapsed_time": 2400,
            "start_latlng": [1.3, 103.8],
            "end_latlng": [1.31, 103.82],
            "source": "historical_backfill",
            "is_following": True,
            "athlete_profile_image_url": None,
        }
        transformed = {
            "stream_status": "ok",
            "privacy_zone_start": False,
            "privacy_zone_end": False,
            "truncation_point_start": None,
            "truncation_point_end": None,
            "path": [[103.8, 1.3, 1712311200], [103.82, 1.31, 1712313600]],
        }
        db.save_activity(conn, activity, transformed)
    finally:
        conn.close()

    client = TestClient(app)
    response = client.get("/api/v1/athletes/9")
    assert response.status_code == 200
    payload = response.json()
    assert payload["athlete_id"] == 9
    assert payload["name"] == "Riley"
    assert payload["is_tracked"] is True
    assert payload["backfill_status"] == "degraded"
    assert payload["backfill_last_issue_code"] == "parse_empty"
    assert payload["backfill_last_issue_message"] == "profile page loaded but no usable activities could be parsed"
    assert payload["backfill_last_issue_at"] == "2026-04-11T08:30:00+00:00"
    assert payload["recent_activities"][0]["activity_id"] == 777
    assert payload["recent_activities"][0]["stream_status"] == "ok"


def test_athlete_routes_api_returns_all_saved_routes(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "athlete-routes.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        db.sync_following_roster(
            conn,
            [{"athlete_id": 14, "name": "Taylor", "avatar_url": None, "source": "following_roster"}],
        )
        transformed = {
            "stream_status": "ok",
            "privacy_zone_start": False,
            "privacy_zone_end": False,
            "truncation_point_start": None,
            "truncation_point_end": None,
            "path": [[103.8, 1.3, 1712311200], [103.82, 1.31, 1712313600]],
        }
        for activity_id, start_utc, start_local in [
            (781, "2026-04-05T10:00:00Z", "2026-04-05T18:00:00+08:00"),
            (782, "2026-04-02T10:00:00Z", "2026-04-02T18:00:00+08:00"),
        ]:
            activity = {
                "activity_id": activity_id,
                "athlete_id": 14,
                "athlete_name": "Taylor",
                "activity_name": f"Route {activity_id}",
                "sport_type": "Ride",
                "start_date_utc": start_utc,
                "start_date_local": start_local,
                "elapsed_time": 2400,
                "start_latlng": [1.3, 103.8],
                "end_latlng": [1.31, 103.82],
                "source": "historical_backfill",
                "is_following": True,
                "athlete_profile_image_url": None,
            }
            db.save_activity(conn, activity, transformed)
    finally:
        conn.close()

    client = TestClient(app)
    response = client.get("/api/v1/athletes/14/routes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["athlete_id"] == 14
    assert payload["name"] == "Taylor"
    assert payload["activity_count"] == 2
    assert len(payload["routes"]) == 2
    assert payload["routes"][0]["activity_id"] == 781
    assert payload["routes"][0]["path"][0] == [103.8, 1.3, 1712311200]


def test_backfill_coverage_api_groups_by_year_and_month(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "coverage.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        db.sync_following_roster(
            conn,
            [{"athlete_id": 12, "name": "Casey", "avatar_url": None, "source": "following_roster"}],
        )
        base_activity = {
            "athlete_id": 12,
            "athlete_name": "Casey",
            "sport_type": "Run",
            "elapsed_time": 1200,
            "start_latlng": [1.3, 103.8],
            "end_latlng": [1.31, 103.81],
            "source": "historical_backfill",
            "is_following": True,
            "athlete_profile_image_url": None,
        }
        transformed = {
            "stream_status": "ok",
            "privacy_zone_start": False,
            "privacy_zone_end": False,
            "truncation_point_start": None,
            "truncation_point_end": None,
            "path": [[103.8, 1.3, 1712376000], [103.81, 1.31, 1712376060]],
        }
        for activity_id, start_utc, start_local in [
            (901, "2026-04-06T04:00:00Z", "2026-04-06T12:00:00+08:00"),
            (902, "2026-03-06T04:00:00Z", "2026-03-06T12:00:00+08:00"),
            (903, "2025-12-06T04:00:00Z", "2025-12-06T12:00:00+08:00"),
        ]:
            activity = {
                **base_activity,
                "activity_id": activity_id,
                "activity_name": f"Run {activity_id}",
                "start_date_utc": start_utc,
                "start_date_local": start_local,
            }
            db.save_activity(conn, activity, transformed)
    finally:
        conn.close()

    client = TestClient(app)
    response = client.get("/api/v1/backfill/coverage")
    assert response.status_code == 200
    payload = response.json()
    assert payload["year_count"] == 2
    assert payload["month_count"] == 3
    assert payload["activity_count"] == 3
    assert payload["years"][0]["year"] == "2026"
    assert payload["years"][0]["months"][0]["month"] == "2026-04"


def test_athletes_api_filters_by_month(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "athletes-month.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        db.sync_following_roster(
            conn,
            [{"athlete_id": 7, "name": "Jordan", "avatar_url": None, "source": "following_roster"}],
        )
        transformed = {
            "stream_status": "ok",
            "privacy_zone_start": False,
            "privacy_zone_end": False,
            "truncation_point_start": None,
            "truncation_point_end": None,
            "path": [[103.8, 1.3, 1712376000], [103.81, 1.31, 1712376060]],
        }
        for activity_id, start_utc, start_local in [
            (601, "2026-04-06T04:00:00Z", "2026-04-06T12:00:00+08:00"),
            (602, "2026-03-06T04:00:00Z", "2026-03-06T12:00:00+08:00"),
        ]:
            activity = {
                "activity_id": activity_id,
                "athlete_id": 7,
                "athlete_name": "Jordan",
                "activity_name": "Run",
                "sport_type": "Run",
                "start_date_utc": start_utc,
                "start_date_local": start_local,
                "elapsed_time": 1200,
                "start_latlng": [1.3, 103.8],
                "end_latlng": [1.31, 103.81],
                "source": "following_feed",
                "is_following": True,
                "athlete_profile_image_url": None,
            }
            db.save_activity(conn, activity, transformed)
    finally:
        conn.close()

    client = TestClient(app)
    response = client.get("/api/v1/athletes?month=2026-03")
    assert response.status_code == 200
    payload = response.json()
    assert payload["athletes"][0]["activity_count"] == 1


def test_backfill_job_api_returns_command() -> None:
    client = TestClient(app)
    response = client.get("/api/v1/backfill/job")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["command"], list)
    assert "-m" in payload["command"]
    assert "--backfill-only" in payload["command"]
    assert "--date" not in payload["command"]


def test_dates_api_returns_saved_dates_from_latest_to_oldest(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "dates.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        db.sync_following_roster(
            conn,
            [{"athlete_id": 3, "name": "Avery", "avatar_url": None, "source": "following_roster"}],
        )
        transformed = {
            "stream_status": "ok",
            "privacy_zone_start": False,
            "privacy_zone_end": False,
            "truncation_point_start": None,
            "truncation_point_end": None,
            "path": [[103.8, 1.3, 1712376000], [103.81, 1.31, 1712376060]],
        }
        for activity_id, start_utc, start_local in [
            (301, "2026-04-06T04:00:00Z", "2026-04-06T12:00:00+08:00"),
            (302, "2026-03-06T04:00:00Z", "2026-03-06T12:00:00+08:00"),
        ]:
            activity = {
                "activity_id": activity_id,
                "athlete_id": 3,
                "athlete_name": "Avery",
                "activity_name": f"Run {activity_id}",
                "sport_type": "Run",
                "start_date_utc": start_utc,
                "start_date_local": start_local,
                "elapsed_time": 1200,
                "start_latlng": [1.3, 103.8],
                "end_latlng": [1.31, 103.81],
                "source": "following_feed",
                "is_following": True,
                "athlete_profile_image_url": None,
            }
            db.save_activity(conn, activity, transformed)
    finally:
        conn.close()

    client = TestClient(app)
    response = client.get("/api/v1/dates")
    assert response.status_code == 200
    assert response.json() == {"dates": ["2026-04-06", "2026-03-06"]}



def test_sync_job_api_returns_default_sync_command() -> None:
    client = TestClient(app)
    response = client.get("/api/v1/sync/job")
    assert response.status_code == 200
    payload = response.json()
    assert payload["running"] is False
    assert isinstance(payload["command"], list)
    assert "--sync-only" in payload["command"]
    assert "--date" in payload["command"]



def test_sync_run_and_stop_api_manage_runner_state() -> None:
    client = TestClient(app)
    started = client.post("/api/v1/sync/run?date=2026-04-12&refresh_following_roster=true")
    assert started.status_code == 200
    start_payload = started.json()
    assert start_payload["running"] is True
    assert "--date" in start_payload["command"]
    assert "2026-04-12" in start_payload["command"]
    assert "--refresh-following-roster" in start_payload["command"]
    assert start_payload["pid"] is not None
    assert start_payload["log_path"]

    stopped = client.post("/api/v1/sync/stop")
    assert stopped.status_code == 200
    stop_payload = stopped.json()
    assert stop_payload["running"] is False
    assert "--date" in stop_payload["command"]



def test_backfill_run_and_stop_api_manage_runner_state() -> None:
    client = TestClient(app)
    started = client.post("/api/v1/backfill/run?steps=4")
    assert started.status_code == 200
    start_payload = started.json()
    assert start_payload["running"] is True
    assert "--backfill-only" in start_payload["command"]
    assert "--backfill-steps" in start_payload["command"]
    assert "4" in start_payload["command"]
    assert start_payload["pid"] is not None
    assert start_payload["log_path"]

    stopped = client.post("/api/v1/backfill/stop")
    assert stopped.status_code == 200
    stop_payload = stopped.json()
    assert stop_payload["running"] is False
    assert "--backfill-only" in stop_payload["command"]
