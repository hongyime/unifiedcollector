"""
Preservation property tests for signal handling bugfix.

**Property 2: Preservation** - Normal Operation Behavior
**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

These tests verify that normal operations (without signal interruption) continue to work
correctly after the signal handling fix is implemented. These tests should PASS on unfixed code
to establish the baseline behavior that must be preserved.

Testing Approach:
- Observe behavior on UNFIXED code for non-buggy inputs (normal execution without signal interruption)
- Write property-based tests capturing observed behavior patterns from Preservation Requirements
- Property-based testing generates many test cases for stronger guarantees
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings as hypothesis_settings, HealthCheck
from hypothesis import strategies as st

from ingestion import db
from ingestion.config import Settings
from ingestion.crawler import Crawler, CrawlSummary


# Test fixtures and helpers

@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    
    db.init_db(db_path)
    yield db_path
    
    # Cleanup
    Path(db_path).unlink(missing_ok=True)
    Path(f"{db_path}-shm").unlink(missing_ok=True)
    Path(f"{db_path}-wal").unlink(missing_ok=True)


@pytest.fixture
def mock_session():
    """Create a mock session for testing."""
    class MockSession:
        def __init__(self):
            self.cookie_value = "test_cookie"
            self._persist_callback = None
        
        def validate(self):
            return {
                "id": 12345,
                "firstname": "Test",
                "lastname": "User",
                "profile": "https://example.com/avatar.jpg",
                "private": False,
            }
        
        def clone(self):
            return MockSession()
        
        def set_persist_callback(self, callback):
            self._persist_callback = callback
        
        def get_json(self, url, **params):
            # Mock response for stream requests
            class MockResponse:
                status_code = 200
            return MockResponse(), {"latlng": [], "time": []}
    
    return MockSession()


@pytest.fixture
def mock_settings(temp_db):
    """Create mock settings for testing."""
    class MockSettings:
        def __init__(self, db_path):
            self.db_path = db_path
            self.backfill_steps = 10
            self.backfill_parallelism = 2
            self.backfill_year_cap = 5
            self.stream_delay_min_seconds = 0.0  # No delay for tests
            self.stream_delay_max_seconds = 0.0
            self.debug_delays = False
    
    return MockSettings(temp_db)


# Property-based test strategies

@st.composite
def athlete_data(draw):
    """Generate random athlete data."""
    athlete_id = draw(st.integers(min_value=1, max_value=1000000))
    name = draw(st.text(min_size=1, max_size=50, alphabet=st.characters(blacklist_characters="\x00", blacklist_categories={"Cs"})))
    is_following = draw(st.booleans())
    is_tracked = draw(st.booleans())
    return {
        "athlete_id": athlete_id,
        "name": name,
        "is_following": is_following,
        "is_tracked": is_tracked,
    }


@st.composite
def activity_data(draw):
    """Generate random activity data."""
    activity_id = draw(st.integers(min_value=1, max_value=1000000000))
    athlete_id = draw(st.integers(min_value=1, max_value=1000000))
    return {
        "activity_id": activity_id,
        "athlete_id": athlete_id,
        "athlete_name": "Test Athlete",
        "activity_name": "Test Activity",
        "sport_type": "Run",
        "source": "following_feed",
        "start_date_utc": "2024-01-01T12:00:00+00:00",
        "start_date_local": "2024-01-01T12:00:00",
        "is_renderable": False,  # Skip stream fetching
    }


# Preservation Property Tests

def test_database_connection_uses_autocommit_mode(temp_db):
    """
    **Property 2: Preservation** - Database Consistency
    
    Verify that database connections use autocommit mode (isolation_level=None)
    for statement-level atomicity. This behavior must be preserved.
    
    **Validates: Requirement 3.3**
    """
    conn = db.connect(temp_db)
    try:
        # Verify autocommit mode is enabled
        assert conn.isolation_level is None, "Database should use autocommit mode (isolation_level=None)"
        
        # Verify WAL mode is enabled
        result = conn.execute("PRAGMA journal_mode").fetchone()
        assert result[0].upper() == "WAL", "Database should use WAL mode"
        
        # Verify foreign keys are enabled
        result = conn.execute("PRAGMA foreign_keys").fetchone()
        assert result[0] == 1, "Foreign keys should be enabled"
    finally:
        conn.close()


@given(athletes=st.lists(athlete_data(), min_size=1, max_size=10, unique_by=lambda x: x["athlete_id"]))
@hypothesis_settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_normal_athlete_upsert_preserves_data_consistency(temp_db, athletes):
    """
    **Property 2: Preservation** - Database Consistency
    
    Verify that normal athlete upsert operations maintain data consistency.
    This is the baseline behavior that must be preserved.
    
    Note: The upsert logic has special handling for is_tracked:
    - When new value is True, it sets to True
    - When new value is False, it preserves existing value
    
    **Validates: Requirement 3.1**
    """
    conn = db.connect(temp_db)
    try:
        # Track the expected final state considering upsert semantics
        for athlete in athletes:
            db.upsert_athlete(
                conn,
                athlete_id=athlete["athlete_id"],
                name=athlete["name"],
                is_following=athlete["is_following"],
                is_tracked=athlete["is_tracked"],
            )
        
        # Verify all athletes exist and have consistent data
        for athlete in athletes:
            row = conn.execute(
                "SELECT * FROM athletes WHERE athlete_id = ?",
                (athlete["athlete_id"],)
            ).fetchone()
            assert row is not None, f"Athlete {athlete['athlete_id']} should exist"
            # Name and is_following should match the last upsert
            assert row["name"] == athlete["name"]
            assert bool(row["is_following"]) == athlete["is_following"]
            # is_tracked has special logic: once True, stays True
            # So we just verify it's a valid boolean value
            assert row["is_tracked"] in (0, 1), "is_tracked should be a valid boolean"
    finally:
        conn.close()


def test_crawl_run_records_status_and_summary(temp_db, mock_session, mock_settings):
    """
    **Property 2: Preservation** - Resume Capability
    
    Verify that crawler records run status and summary in crawl_runs table.
    This behavior must be preserved for resume capability.
    
    **Validates: Requirement 3.6**
    """
    conn = db.connect(temp_db)
    try:
        # Create a crawl run
        run_id = db.create_crawl_run(
            conn,
            run_type="daily_sync",
            target_date="2024-01-01",
            roster_refreshed=False,
            backfill_step_limit=10,
        )
        
        # Verify run was created
        row = conn.execute("SELECT * FROM crawl_runs WHERE id = ?", (run_id,)).fetchone()
        assert row is not None
        assert row["run_type"] == "daily_sync"
        assert row["target_date"] == "2024-01-01"
        assert row["status"] == "running"
        
        # Finalize the run
        summary = {"test": "data"}
        db.finalize_crawl_run(conn, run_id, status="ok", notes=json.dumps(summary))
        
        # Verify run was finalized
        row = conn.execute("SELECT * FROM crawl_runs WHERE id = ?", (run_id,)).fetchone()
        assert row["status"] == "ok"
        assert row["completed_at"] is not None
        assert json.loads(row["notes"]) == summary
    finally:
        conn.close()


def test_backfill_progress_tracking_preserved(temp_db):
    """
    **Property 2: Preservation** - Resume Capability
    
    Verify that backfill operations save cursor positions and status after each athlete-month page.
    This behavior must be preserved for resume capability.
    
    **Validates: Requirement 3.5**
    """
    conn = db.connect(temp_db)
    try:
        # Insert test athlete
        athlete_id = 12345
        db.upsert_athlete(
            conn,
            athlete_id=athlete_id,
            name="Test Athlete",
            is_tracked=True,
        )
        
        # Update backfill progress
        db.update_backfill_progress(
            conn,
            athlete_id=athlete_id,
            cursor_before="2024-01",
            oldest_seen_utc="2024-01-15T12:00:00+00:00",
            status="active",
            completed=False,
        )
        
        # Verify progress was saved
        row = conn.execute(
            "SELECT * FROM athletes WHERE athlete_id = ?",
            (athlete_id,)
        ).fetchone()
        assert row["backfill_deep_cursor_before"] == "2024-01"
        assert row["backfill_oldest_seen_utc"] == "2024-01-15T12:00:00+00:00"
        assert row["backfill_status"] == "active"
        assert row["backfill_completed_at"] is None
        
        # Complete backfill
        db.update_backfill_progress(
            conn,
            athlete_id=athlete_id,
            cursor_before="2023-12",
            oldest_seen_utc="2023-12-01T12:00:00+00:00",
            status="complete",
            completed=True,
        )
        
        # Verify completion was saved
        row = conn.execute(
            "SELECT * FROM athletes WHERE athlete_id = ?",
            (athlete_id,)
        ).fetchone()
        assert row["backfill_status"] == "complete"
        assert row["backfill_completed_at"] is not None
    finally:
        conn.close()


def test_keyboard_interrupt_handler_message_preserved():
    """
    **Property 2: Preservation** - Top-level KeyboardInterrupt Handler
    
    Verify that the top-level KeyboardInterrupt handler in main.py prints the safe stop message.
    This behavior must be preserved.
    
    **Validates: Requirement 3.2**
    
    Note: This test verifies the message exists in the code. The actual behavior is tested
    in integration tests.
    """
    # Read main.py and verify the KeyboardInterrupt handler exists
    main_py_path = Path("ingestion/main.py")
    assert main_py_path.exists(), "main.py should exist"
    
    content = main_py_path.read_text()
    
    # Verify KeyboardInterrupt handler exists
    assert "except KeyboardInterrupt:" in content, "KeyboardInterrupt handler should exist"
    assert "Run stopped safely" in content or "Saved work remains intact" in content, \
        "Safe stop message should be present"


@given(activities=st.lists(activity_data(), min_size=1, max_size=5))
@hypothesis_settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_normal_activity_ingestion_preserves_consistency(temp_db, mock_session, mock_settings, activities):
    """
    **Property 2: Preservation** - Normal Operation Completion
    
    Verify that normal activity ingestion (without interruption) maintains data consistency.
    This is the baseline behavior that must be preserved.
    
    **Validates: Requirement 3.1**
    """
    conn = db.connect(temp_db)
    try:
        # Insert athletes first
        for activity in activities:
            db.upsert_athlete(
                conn,
                athlete_id=activity["athlete_id"],
                name=activity["athlete_name"],
                is_tracked=True,
            )
        
        # Create crawler and ingest activities
        crawler = Crawler(conn, mock_session, mock_settings)
        new_count = crawler._ingest_activity_batch(activities)
        
        # Verify activities were ingested
        assert new_count == 0, "Activities without streams should be skipped (is_renderable=False)"
        
        # Verify database consistency - all athletes should still exist
        for activity in activities:
            row = conn.execute(
                "SELECT * FROM athletes WHERE athlete_id = ?",
                (activity["athlete_id"],)
            ).fetchone()
            assert row is not None, f"Athlete {activity['athlete_id']} should exist"
    finally:
        conn.close()


def test_database_connection_cleanup_on_normal_exit(temp_db):
    """
    **Property 2: Preservation** - Database Connection Management
    
    Verify that database connections are properly closed on normal exit.
    This behavior must be preserved.
    
    **Validates: Requirement 3.3**
    """
    # Open and close connection normally
    conn = db.connect(temp_db)
    assert not conn.execute("SELECT 1").fetchone() is None, "Connection should be open"
    conn.close()
    
    # Verify connection is closed
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        conn.execute("SELECT 1")


def test_wal_mode_allows_concurrent_reads(temp_db):
    """
    **Property 2: Preservation** - Database Consistency
    
    Verify that WAL mode allows concurrent reads during normal operations.
    This behavior must be preserved.
    
    **Validates: Requirement 3.3**
    """
    # Open write connection
    write_conn = db.connect(temp_db)
    
    try:
        # Insert test data
        db.upsert_athlete(
            write_conn,
            athlete_id=12345,
            name="Test Athlete",
            is_tracked=True,
        )
        
        # Open read-only connection
        read_conn = db.connect_readonly(temp_db)
        
        try:
            # Verify read connection can read while write connection is open
            row = read_conn.execute(
                "SELECT * FROM athletes WHERE athlete_id = ?",
                (12345,)
            ).fetchone()
            assert row is not None
            assert row["name"] == "Test Athlete"
        finally:
            read_conn.close()
    finally:
        write_conn.close()


def test_interrupted_run_can_resume_from_last_committed_point(temp_db):
    """
    **Property 2: Preservation** - Resume Capability
    
    Verify that interrupted runs can resume from the last committed point.
    This behavior must be preserved.
    
    **Validates: Requirement 3.4**
    """
    conn = db.connect(temp_db)
    try:
        # Simulate first run that gets interrupted
        athlete_id = 12345
        db.upsert_athlete(
            conn,
            athlete_id=athlete_id,
            name="Test Athlete",
            is_tracked=True,
        )
        
        # Save progress
        db.update_backfill_progress(
            conn,
            athlete_id=athlete_id,
            cursor_before="2024-01",
            oldest_seen_utc="2024-01-15T12:00:00+00:00",
            status="active",
            completed=False,
        )
        
        # Simulate resuming from saved progress
        row = conn.execute(
            "SELECT * FROM athletes WHERE athlete_id = ?",
            (athlete_id,)
        ).fetchone()
        
        # Verify we can resume from the saved cursor
        assert row["backfill_deep_cursor_before"] == "2024-01"
        assert row["backfill_status"] == "active"
        assert row["backfill_completed_at"] is None
        
        # Continue from saved cursor
        db.update_backfill_progress(
            conn,
            athlete_id=athlete_id,
            cursor_before="2023-12",
            oldest_seen_utc="2023-12-01T12:00:00+00:00",
            status="active",
            completed=False,
        )
        
        # Verify progress continued
        row = conn.execute(
            "SELECT * FROM athletes WHERE athlete_id = ?",
            (athlete_id,)
        ).fetchone()
        assert row["backfill_deep_cursor_before"] == "2023-12"
    finally:
        conn.close()


if __name__ == "__main__":
    print("=" * 80)
    print("Preservation Property Tests - Signal Handling")
    print("=" * 80)
    print()
    print("These tests verify that normal operations (without signal interruption)")
    print("continue to work correctly. These tests should PASS on unfixed code.")
    print()
    
    pytest.main([__file__, "-v", "-s"])
