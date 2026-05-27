"""Test shutdown propagation to worker threads in _backfill_athlete()."""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from ingestion import crawler, db


def _build_settings():
    class MockSettings:
        backfill_steps = 10
        backfill_parallelism = 1
        timezone = None
        stream_delay_min_seconds = 1.0
        stream_delay_max_seconds = 2.5
        debug_delays = False

    return MockSettings()


def test_shutdown_during_backfill_athlete_loop(tmp_path: Path) -> None:
    """Test that shutdown_event is checked inside _backfill_athlete() loop before processing each month-page."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        settings = _build_settings()
        settings.db_path = db_path

        # Insert a test athlete
        conn.execute(
            """INSERT INTO athletes (athlete_id, name, is_following, backfill_deep_cursor_before, backfill_oldest_seen_utc, backfill_status, first_seen_at, last_seen_at)
               VALUES (123, 'Test Athlete', 1, NULL, NULL, 'active', '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')"""
        )
        conn.commit()

        class FakeSession:
            def validate(self):
                return {"id": 9, "firstname": "Test", "lastname": "User"}

        # Create shutdown_event
        shutdown_event = threading.Event()
        
        runner = crawler.Crawler(conn, FakeSession(), settings, shutdown_event)
        
        # Mock the history_scraper to simulate multiple month-pages
        call_count = 0
        def mock_fetch_batch(athlete_id, cursor_before, oldest_seen, is_following):
            nonlocal call_count
            call_count += 1
            
            # Set shutdown_event after first call to simulate shutdown during loop
            if call_count == 1:
                shutdown_event.set()
            
            # Return some activities for the first call
            return (
                [{"activity_id": 1, "start_date_utc": "2024-01-01T00:00:00Z", "athlete_name": "Test Athlete", "activity_name": "Test Activity"}],
                "2024-01",
                "active",
                None
            )
        
        runner.history_scraper = MagicMock()
        runner.history_scraper.fetch_batch = mock_fetch_batch
        
        # Mock _ingest_activity_batch to avoid needing to mock the entire session
        runner._ingest_activity_batch = lambda activities: len(activities)
        
        # Run backfill with max_steps=5 (should stop early due to shutdown)
        result = runner._backfill_athlete(123, max_steps=5)
        
        # Verify that the function returned early due to shutdown
        assert result["completed"] is False
        assert result["degraded"] is False
        assert result["steps_used"] == 1  # Only processed one month-page before shutdown
        
        # Verify that progress was saved
        athlete = conn.execute("SELECT * FROM athletes WHERE athlete_id = 123").fetchone()
        assert athlete["backfill_status"] == "active"
        assert athlete["backfill_completed_at"] is None
        
    finally:
        conn.close()


def test_no_shutdown_backfill_athlete_completes_normally(tmp_path: Path) -> None:
    """Test that _backfill_athlete() completes normally when shutdown_event is not set."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        settings = _build_settings()
        settings.db_path = db_path

        # Insert a test athlete
        conn.execute(
            """INSERT INTO athletes (athlete_id, name, is_following, backfill_deep_cursor_before, backfill_oldest_seen_utc, backfill_status, first_seen_at, last_seen_at)
               VALUES (456, 'Test Athlete 2', 1, NULL, NULL, 'active', '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')"""
        )
        conn.commit()

        class FakeSession:
            def validate(self):
                return {"id": 9, "firstname": "Test", "lastname": "User"}

        # Create shutdown_event but don't set it
        shutdown_event = threading.Event()
        
        runner = crawler.Crawler(conn, FakeSession(), settings, shutdown_event)
        
        # Mock the history_scraper to return complete status
        def mock_fetch_batch(athlete_id, cursor_before, oldest_seen, is_following):
            return (
                [{"activity_id": 2, "start_date_utc": "2024-01-01T00:00:00Z", "athlete_name": "Test Athlete 2", "activity_name": "Test Activity"}],
                "2024-01",
                "complete",
                None
            )
        
        runner.history_scraper = MagicMock()
        runner.history_scraper.fetch_batch = mock_fetch_batch
        
        # Mock _ingest_activity_batch to avoid needing to mock the entire session
        runner._ingest_activity_batch = lambda activities: len(activities)
        
        # Run backfill with max_steps=1
        result = runner._backfill_athlete(456, max_steps=1)
        
        # Verify that the function completed normally
        assert result["completed"] is True
        assert result["degraded"] is False
        assert result["steps_used"] == 1
        
        # Verify that progress was saved as complete
        athlete = conn.execute("SELECT * FROM athletes WHERE athlete_id = 456").fetchone()
        assert athlete["backfill_status"] == "complete"
        assert athlete["backfill_completed_at"] is not None
        
    finally:
        conn.close()


def test_shutdown_event_none_backfill_athlete_works(tmp_path: Path) -> None:
    """Test that _backfill_athlete() works when shutdown_event is None (backward compatibility)."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        settings = _build_settings()
        settings.db_path = db_path

        # Insert a test athlete
        conn.execute(
            """INSERT INTO athletes (athlete_id, name, is_following, backfill_deep_cursor_before, backfill_oldest_seen_utc, backfill_status, first_seen_at, last_seen_at)
               VALUES (789, 'Test Athlete 3', 1, NULL, NULL, 'active', '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')"""
        )
        conn.commit()

        class FakeSession:
            def validate(self):
                return {"id": 9, "firstname": "Test", "lastname": "User"}

        # Don't provide shutdown_event (None)
        runner = crawler.Crawler(conn, FakeSession(), settings, None)
        
        # Mock the history_scraper to return complete status
        def mock_fetch_batch(athlete_id, cursor_before, oldest_seen, is_following):
            return (
                [{"activity_id": 3, "start_date_utc": "2024-01-01T00:00:00Z", "athlete_name": "Test Athlete 3", "activity_name": "Test Activity"}],
                "2024-01",
                "complete",
                None
            )
        
        runner.history_scraper = MagicMock()
        runner.history_scraper.fetch_batch = mock_fetch_batch
        
        # Mock _ingest_activity_batch to avoid needing to mock the entire session
        runner._ingest_activity_batch = lambda activities: len(activities)
        
        # Run backfill with max_steps=1
        result = runner._backfill_athlete(789, max_steps=1)
        
        # Verify that the function completed normally
        assert result["completed"] is True
        assert result["degraded"] is False
        assert result["steps_used"] == 1
        
    finally:
        conn.close()
