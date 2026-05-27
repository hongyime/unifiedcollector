"""Test shutdown checks in crawler.py run() method."""
from __future__ import annotations

import threading
from pathlib import Path

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


def test_shutdown_before_daily_sync(tmp_path: Path) -> None:
    """Test that shutdown_event is checked before starting daily sync."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        settings = _build_settings()
        settings.db_path = db_path

        class FakeSession:
            def validate(self):
                return {"id": 9, "firstname": "Test", "lastname": "User"}

        # Create shutdown_event and set it
        shutdown_event = threading.Event()
        shutdown_event.set()
        
        runner = crawler.Crawler(conn, FakeSession(), settings, shutdown_event)
        summary = runner.run("2026-04-12", sync_only=True)

        # Verify that the run was aborted
        assert summary.daily_activity_count == 0
        assert summary.new_activity_count == 0
        
        # Verify that the crawl_run was finalized with status='aborted'
        latest_run = conn.execute("SELECT status FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert latest_run["status"] == "aborted"
    finally:
        conn.close()


def test_shutdown_before_backfill(tmp_path: Path) -> None:
    """Test that shutdown_event is checked before starting backfill."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        settings = _build_settings()
        settings.db_path = db_path

        class FakeSession:
            def validate(self):
                return {"id": 9, "firstname": "Test", "lastname": "User"}

        # Create shutdown_event and set it
        shutdown_event = threading.Event()
        shutdown_event.set()
        
        runner = crawler.Crawler(conn, FakeSession(), settings, shutdown_event)
        summary = runner.run("2026-04-12", backfill_only=True)

        # Verify that the run was aborted
        assert summary.backfill_athletes_processed == 0
        assert summary.new_activity_count == 0
        
        # Verify that the crawl_run was finalized with status='aborted'
        latest_run = conn.execute("SELECT status FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert latest_run["status"] == "aborted"
    finally:
        conn.close()


def test_no_shutdown_normal_operation(tmp_path: Path) -> None:
    """Test that normal operation continues when shutdown_event is not set."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        settings = _build_settings()
        settings.db_path = db_path
        settings.backfill_steps = 0  # Skip backfill

        class FakeSession:
            def validate(self):
                return {"id": 9, "firstname": "Test", "lastname": "User"}

        class FakeFeedScraper:
            def fetch_activities_for_date(self, athlete_id: int, target_date: str | None):
                return []

        # Create shutdown_event but don't set it
        shutdown_event = threading.Event()
        
        runner = crawler.Crawler(conn, FakeSession(), settings, shutdown_event)
        runner.feed_scraper = FakeFeedScraper()
        runner._run_backfill = lambda step_limit: []
        
        summary = runner.run("2026-04-12")

        # Verify that the run completed normally
        latest_run = conn.execute("SELECT status FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert latest_run["status"] == "ok"
    finally:
        conn.close()


def test_no_shutdown_event_provided(tmp_path: Path) -> None:
    """Test that normal operation continues when shutdown_event is None."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        settings = _build_settings()
        settings.db_path = db_path
        settings.backfill_steps = 0  # Skip backfill

        class FakeSession:
            def validate(self):
                return {"id": 9, "firstname": "Test", "lastname": "User"}

        class FakeFeedScraper:
            def fetch_activities_for_date(self, athlete_id: int, target_date: str | None):
                return []

        # Don't provide shutdown_event (None)
        runner = crawler.Crawler(conn, FakeSession(), settings, None)
        runner.feed_scraper = FakeFeedScraper()
        runner._run_backfill = lambda step_limit: []
        
        summary = runner.run("2026-04-12")

        # Verify that the run completed normally
        latest_run = conn.execute("SELECT status FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert latest_run["status"] == "ok"
    finally:
        conn.close()
