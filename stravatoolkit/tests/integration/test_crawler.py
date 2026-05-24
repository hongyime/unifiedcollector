from __future__ import annotations

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


def test_backfill_degraded_status_advances_cursor(tmp_path: Path) -> None:
    """Test that degraded status advances the cursor instead of getting stuck."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        # Setup: Add athlete to tracked roster
        db.sync_following_roster(
            conn,
            [{"athlete_id": 42, "name": "Test Athlete", "avatar_url": None, "source": "following_roster"}],
        )

        # Mock the history scraper to return degraded status with next_cursor
        class MockHistoryScraper:
            def fetch_batch(self, athlete_id: int, cursor_before: str | None, oldest_seen_utc: str | None, *, is_following: bool):
                # Simulate parse_empty degradation: entries exist but no parsed activities
                # Returns next_cursor indicating the next month to try
                return [], "202507", "degraded", None

        # Create crawler runner
        settings = _build_settings()
        cr = crawler.Crawler(conn, None, settings)
        cr.history_scraper = MockHistoryScraper()

        # Run backfill for this athlete
        result = cr._backfill_athlete(42, max_steps=1)

        # Verify: Degraded status should advance cursor to next_cursor
        athlete = conn.execute(
            "SELECT * FROM athletes WHERE athlete_id = ?",
            (42,),
        ).fetchone()

        # The key assertion: deep cursor should be ADVANCED to the next month, not stuck on current month
        assert athlete["backfill_deep_cursor_before"] == "202507", (
            f"Expected deep cursor to advance to '202507', but got '{athlete['backfill_deep_cursor_before']}'. "
            "Degraded status should advance deep cursor like 'active' and 'gap' statuses."
        )

        # Verify other expected fields
        assert athlete["backfill_status"] == "degraded"
        assert athlete["backfill_completed_at"] is None
        assert result["degraded"] is True
        assert result["completed"] is False
        assert result["steps_used"] == 1

    finally:
        conn.close()


def test_run_marks_daily_sync_as_degraded_when_feed_fetch_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "run-degraded.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        settings = _build_settings()
        settings.db_path = db_path
        settings.backfill_steps = 1

        class FakeSession:
            def validate(self):
                return {"id": 9, "firstname": "Riley", "lastname": "Tan"}

        class BrokenFeedScraper:
            def fetch_activities_for_date(self, athlete_id: int, target_date: str | None):
                raise RuntimeError("feed unavailable")

        runner = crawler.Crawler(conn, FakeSession(), settings)
        runner.feed_scraper = BrokenFeedScraper()
        runner._run_backfill = lambda step_limit: []

        summary = runner.run("2026-04-12")

        assert summary.daily_sync_degraded is True
        assert summary.daily_sync_issue == "feed unavailable"
        latest_run = conn.execute("SELECT status, notes FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert latest_run["status"] == "ok"
        assert '"daily_sync_degraded": true' in latest_run["notes"]
    finally:
        conn.close()


def test_backfill_active_status_advances_cursor(tmp_path: Path) -> None:
    """Test that active status advances the cursor (baseline test)."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        db.sync_following_roster(
            conn,
            [{"athlete_id": 43, "name": "Active Athlete", "avatar_url": None, "source": "following_roster"}],
        )

        class MockHistoryScraper:
            def fetch_batch(self, athlete_id: int, cursor_before: str | None, oldest_seen_utc: str | None, *, is_following: bool):
                # Return some activities with active status
                return [{"activity_id": 1, "start_date_utc": "2025-08-15T00:00:00Z"}], "202507", "active", None

        settings = _build_settings()
        cr = crawler.Crawler(conn, None, settings)
        cr.history_scraper = MockHistoryScraper()

        # Mock the activity ingestion since we're not testing that here
        original_ingest = cr._ingest_activity_batch
        cr._ingest_activity_batch = lambda activities: 1

        result = cr._backfill_athlete(43, max_steps=1)

        athlete = conn.execute(
            "SELECT * FROM athletes WHERE athlete_id = ?",
            (43,),
        ).fetchone()

        # Active status should advance deep cursor
        assert athlete["backfill_deep_cursor_before"] == "202507"
        assert athlete["backfill_status"] == "active"
        assert result["degraded"] is False
        assert result["completed"] is False

    finally:
        conn.close()


def test_backfill_gap_status_advances_cursor(tmp_path: Path) -> None:
    """Test that gap status (no activities in month) advances the cursor."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        db.sync_following_roster(
            conn,
            [{"athlete_id": 44, "name": "Gap Athlete", "avatar_url": None, "source": "following_roster"}],
        )

        class MockHistoryScraper:
            def fetch_batch(self, athlete_id: int, cursor_before: str | None, oldest_seen_utc: str | None, *, is_following: bool):
                # Return gap status (month loaded but no visible activities)
                return [], "202507", "gap", None

        settings = _build_settings()
        cr = crawler.Crawler(conn, None, settings)
        cr.history_scraper = MockHistoryScraper()

        result = cr._backfill_athlete(44, max_steps=1)

        athlete = conn.execute(
            "SELECT * FROM athletes WHERE athlete_id = ?",
            (44,),
        ).fetchone()

        # Gap status should advance deep cursor
        assert athlete["backfill_deep_cursor_before"] == "202507"
        assert athlete["backfill_status"] == "gap"
        assert result["degraded"] is False
        assert result["completed"] is False

    finally:
        conn.close()
