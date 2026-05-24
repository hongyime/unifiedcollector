"""Property tests for invalid_usernames database schema.

Property 17: Timestamp Validity
Property 18: Unique Constraint Enforcement
"""

import os
import sqlite3
import tempfile
import time
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.invalid_username_tracker import InvalidUsernameTracker
from src.models import InvalidReason


def _make_tracker() -> InvalidUsernameTracker:
    """Create a tracker backed by a fresh temp file (safe for @given)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return InvalidUsernameTracker(db_path=path)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tracker(tmp_path):
    """Create a fresh tracker backed by a temp database."""
    db_path = tmp_path / "test_tracker.db"
    return InvalidUsernameTracker(db_path=str(db_path))


# ── Property 18: Unique Constraint Enforcement ────────────────────────────────

class TestUniqueConstraintEnforcement:
    """Property 18: Duplicate (username, detected_at) pairs are rejected.

    Validates: Requirements 8.2
    """

    def test_duplicate_username_detected_at_raises(self, tmp_path):
        """Inserting the same (username, detected_at) twice must fail."""
        db_path = tmp_path / "schema_test.db"
        tracker = InvalidUsernameTracker(db_path=str(db_path))

        username = "testuser"
        detected_at = time.time()

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO invalid_usernames (username, reason, detected_at) VALUES (?, ?, ?)",
            (username, "not_found", detected_at),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO invalid_usernames (username, reason, detected_at) VALUES (?, ?, ?)",
                (username, "not_found", detected_at),
            )
            conn.commit()

        conn.close()

    @given(
        username=st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
            min_size=1,
            max_size=30,
        ),
        reason=st.sampled_from(list(InvalidReason)),
    )
    @settings(max_examples=30)
    def test_different_detected_at_allows_multiple_records(self, username, reason):
        """Same username with different detected_at timestamps must be allowed."""
        # Use in-memory DB so no tmp_path fixture is needed
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invalid_usernames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                reason TEXT NOT NULL,
                error_message TEXT,
                detected_at REAL NOT NULL,
                retry_count INTEGER DEFAULT 0,
                created_at REAL DEFAULT (unixepoch()),
                UNIQUE(username, detected_at)
            )
        """)
        conn.commit()

        t1 = 1000000.0
        t2 = 1000001.0  # different timestamp

        conn.execute(
            "INSERT INTO invalid_usernames (username, reason, detected_at) VALUES (?, ?, ?)",
            (username, reason.value, t1),
        )
        conn.execute(
            "INSERT INTO invalid_usernames (username, reason, detected_at) VALUES (?, ?, ?)",
            (username, reason.value, t2),
        )
        conn.commit()

        cur = conn.execute(
            "SELECT COUNT(*) FROM invalid_usernames WHERE username=?", (username,)
        )
        count = cur.fetchone()[0]
        assert count == 2
        conn.close()

    def test_schema_has_required_columns(self, tmp_path):
        """The invalid_usernames table must have all required columns."""
        db_path = tmp_path / "col_test.db"
        tracker = InvalidUsernameTracker(db_path=str(db_path))

        conn = sqlite3.connect(str(db_path))
        cur = conn.execute("PRAGMA table_info(invalid_usernames)")
        columns = {row[1] for row in cur.fetchall()}
        conn.close()

        required = {"id", "username", "reason", "error_message", "detected_at", "retry_count", "created_at"}
        assert required.issubset(columns), f"Missing columns: {required - columns}"

    def test_schema_has_username_index(self, tmp_path):
        """An index on the username column must exist."""
        db_path = tmp_path / "idx_test.db"
        tracker = InvalidUsernameTracker(db_path=str(db_path))

        conn = sqlite3.connect(str(db_path))
        cur = conn.execute("PRAGMA index_list(invalid_usernames)")
        indexes = [row[1] for row in cur.fetchall()]
        conn.close()

        assert any("username" in idx for idx in indexes), (
            f"No username index found. Indexes: {indexes}"
        )

    def test_schema_has_detected_at_index(self, tmp_path):
        """An index on the detected_at column must exist."""
        db_path = tmp_path / "idx_test2.db"
        tracker = InvalidUsernameTracker(db_path=str(db_path))

        conn = sqlite3.connect(str(db_path))
        cur = conn.execute("PRAGMA index_list(invalid_usernames)")
        indexes = [row[1] for row in cur.fetchall()]
        conn.close()

        assert any("detected_at" in idx for idx in indexes), (
            f"No detected_at index found. Indexes: {indexes}"
        )


# ── Property 17: Timestamp Validity ──────────────────────────────────────────

class TestTimestampValidity:
    """Property 17: Inserted records have timestamps within 1 second of current time.

    Validates: Requirements 2.1, 8.5
    """

    def test_record_timestamp_within_one_second(self, tracker):
        """detected_at and created_at must be within 1 second of insertion time."""
        before = time.time()
        tracker.record_invalid("timestampuser", InvalidReason.NOT_FOUND)
        after = time.time()

        conn = sqlite3.connect(str(tracker.db_path))
        cur = conn.execute(
            "SELECT detected_at, created_at FROM invalid_usernames WHERE username=?",
            ("timestampuser",),
        )
        row = cur.fetchone()
        conn.close()

        assert row is not None, "Record was not inserted"
        detected_at, created_at = row

        assert before - 1.0 <= detected_at <= after + 1.0, (
            f"detected_at={detected_at} not within 1s of [{before}, {after}]"
        )
        assert before - 1.0 <= created_at <= after + 1.0, (
            f"created_at={created_at} not within 1s of [{before}, {after}]"
        )

    @given(
        usernames=st.lists(
            st.text(
                alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
                min_size=1,
                max_size=20,
            ),
            min_size=1,
            max_size=5,
            unique=True,
        ),
        reason=st.sampled_from(list(InvalidReason)),
    )
    @settings(max_examples=20)
    def test_all_records_have_valid_timestamps(self, usernames, reason):
        """For any set of recorded usernames, all timestamps must be valid Unix timestamps."""
        tracker = _make_tracker()

        before = time.time()
        for username in usernames:
            tracker.record_invalid(username, reason)
        after = time.time()

        conn = sqlite3.connect(str(tracker.db_path))
        cur = conn.execute("SELECT username, detected_at, created_at FROM invalid_usernames")
        rows = cur.fetchall()
        conn.close()

        assert len(rows) == len(usernames)
        for username, detected_at, created_at in rows:
            assert detected_at > 0, f"detected_at must be positive for {username}"
            assert created_at > 0, f"created_at must be positive for {username}"
            assert before - 1.0 <= detected_at <= after + 1.0, (
                f"detected_at={detected_at} out of range for {username}"
            )
