"""Property tests for InvalidUsernameTracker.

Property 4:  Invalid Username Persistence
Property 5:  Confirmation Threshold
Property 6:  Flush Persistence
Property 7:  Clear Username Removal
Property 19: Immediate In-Memory Update
Property 20: Detailed Records Retrieval

Note: @given tests use in-memory SQLite (":memory:") to avoid the
function-scoped fixture health check from Hypothesis.
"""

import sqlite3
import tempfile
import time
import os
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.invalid_username_tracker import InvalidUsernameTracker
from src.models import InvalidReason, InvalidUsernameRecord


# ── Strategies ────────────────────────────────────────────────────────────────

_username_st = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
    min_size=1,
    max_size=30,
)

_reason_st = st.sampled_from(list(InvalidReason))

_usernames_st = st.lists(_username_st, min_size=1, max_size=10, unique=True)


def _make_tracker() -> InvalidUsernameTracker:
    """Create a tracker backed by a fresh temp file (safe for @given)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # let tracker create it fresh
    return InvalidUsernameTracker(db_path=path)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tracker(tmp_path):
    return InvalidUsernameTracker(db_path=str(tmp_path / "tracker.db"))


# ── Property 4: Invalid Username Persistence ──────────────────────────────────

class TestInvalidUsernamePersistence:
    """Property 4: Recorded usernames appear in both DB and in-memory set.

    Validates: Requirements 1.4, 1.5, 2.1, 10.2
    """

    @given(username=_username_st, reason=_reason_st)
    @settings(max_examples=30)
    def test_recorded_username_in_memory_set(self, username, reason):
        tracker = _make_tracker()
        tracker.record_invalid(username, reason)
        assert username in tracker.get_invalid_usernames()

    @given(username=_username_st, reason=_reason_st)
    @settings(max_examples=30)
    def test_recorded_username_in_database(self, username, reason):
        tracker = _make_tracker()
        tracker.record_invalid(username, reason)

        conn = sqlite3.connect(str(tracker.db_path))
        cur = conn.execute(
            "SELECT COUNT(*) FROM invalid_usernames WHERE username=?", (username,)
        )
        count = cur.fetchone()[0]
        conn.close()
        assert count >= 1

    @given(username=_username_st, reason=_reason_st, msg=st.one_of(st.none(), st.text(max_size=100)))
    @settings(max_examples=20)
    def test_recorded_username_has_correct_reason(self, username, reason, msg):
        tracker = _make_tracker()
        tracker.record_invalid(username, reason, error_message=msg)

        conn = sqlite3.connect(str(tracker.db_path))
        cur = conn.execute(
            "SELECT reason FROM invalid_usernames WHERE username=?", (username,)
        )
        row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row[0] == reason.value


# ── Property 19: Immediate In-Memory Update ───────────────────────────────────

class TestImmediateInMemoryUpdate:
    """Property 19: get_invalid_usernames() returns newly recorded username immediately.

    Validates: Requirements 10.2
    """

    @given(username=_username_st, reason=_reason_st)
    @settings(max_examples=30)
    def test_username_available_immediately_after_record(self, username, reason):
        tracker = _make_tracker()
        tracker.record_invalid(username, reason)
        # No flush, no reconnect — must be available immediately
        assert username in tracker.get_invalid_usernames()

    def test_multiple_usernames_all_available_immediately(self, tracker):
        usernames = ["alice", "bob", "charlie", "dave"]
        for u in usernames:
            tracker.record_invalid(u, InvalidReason.NOT_FOUND)
        result = tracker.get_invalid_usernames()
        for u in usernames:
            assert u in result


# ── Property 5: Confirmation Threshold ───────────────────────────────────────

class TestConfirmationThreshold:
    """Property 5: is_confirmed_invalid returns True iff count >= min_detections.

    Validates: Requirements 2.2
    """

    def test_single_detection_not_confirmed_with_default_threshold(self, tracker):
        tracker.record_invalid("singleuser", InvalidReason.NOT_FOUND)
        assert not tracker.is_confirmed_invalid("singleuser")  # default min=2

    def test_two_detections_confirmed_with_default_threshold(self, tmp_path):
        db_path = tmp_path / "conf.db"
        tracker = InvalidUsernameTracker(db_path=str(db_path))
        # Insert two records with different timestamps directly
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO invalid_usernames (username, reason, detected_at) VALUES (?, ?, ?)",
            ("doubleuser", "not_found", time.time()),
        )
        conn.execute(
            "INSERT INTO invalid_usernames (username, reason, detected_at) VALUES (?, ?, ?)",
            ("doubleuser", "not_found", time.time() + 1),
        )
        conn.commit()
        conn.close()
        assert tracker.is_confirmed_invalid("doubleuser")

    @given(
        username=_username_st,
        n_detections=st.integers(min_value=1, max_value=10),
        min_detections=st.integers(min_value=1, max_value=10),
    )
    @settings(max_examples=40, deadline=None)
    def test_threshold_boundary(self, username, n_detections, min_detections):
        tracker = _make_tracker()

        # Insert n_detections records with distinct timestamps
        conn = sqlite3.connect(str(tracker.db_path))
        for i in range(n_detections):
            conn.execute(
                "INSERT INTO invalid_usernames (username, reason, detected_at) VALUES (?, ?, ?)",
                (username, "not_found", float(1_000_000 + i)),
            )
        conn.commit()
        conn.close()

        result = tracker.is_confirmed_invalid(username, min_detections=min_detections)
        expected = n_detections >= min_detections
        assert result == expected

    def test_unknown_username_not_confirmed(self, tracker):
        assert not tracker.is_confirmed_invalid("nonexistent_user_xyz")


# ── Property 6: Flush Persistence ────────────────────────────────────────────

class TestFlushPersistence:
    """Property 6: Records are retrievable after flush() and DB reconnection.

    Validates: Requirements 2.3
    """

    @given(usernames=_usernames_st, reason=_reason_st)
    @settings(max_examples=20)
    def test_records_survive_flush_and_reconnect(self, usernames, reason):
        tracker = _make_tracker()
        db_path = str(tracker.db_path)

        for u in usernames:
            tracker.record_invalid(u, reason)

        tracker.flush()

        # Reconnect with a fresh tracker instance
        tracker2 = InvalidUsernameTracker(db_path=db_path)
        records = tracker2.get_invalid_records()
        recorded_usernames = {r.username for r in records}

        for u in usernames:
            assert u in recorded_usernames, f"{u} not found after flush+reconnect"

    def test_flush_does_not_raise(self, tracker):
        tracker.record_invalid("flushtest", InvalidReason.UNKNOWN)
        tracker.flush()  # must not raise


# ── Property 7: Clear Username Removal ───────────────────────────────────────

class TestClearUsernameRemoval:
    """Property 7: Cleared usernames don't appear in queries or in-memory set.

    Validates: Requirements 2.4
    """

    @given(username=_username_st, reason=_reason_st)
    @settings(max_examples=30)
    def test_cleared_username_not_in_memory(self, username, reason):
        tracker = _make_tracker()
        tracker.record_invalid(username, reason)
        assert username in tracker.get_invalid_usernames()

        tracker.clear_username(username)
        assert username not in tracker.get_invalid_usernames()

    @given(username=_username_st, reason=_reason_st)
    @settings(max_examples=30)
    def test_cleared_username_not_in_database(self, username, reason):
        tracker = _make_tracker()
        tracker.record_invalid(username, reason)
        tracker.clear_username(username)

        conn = sqlite3.connect(str(tracker.db_path))
        cur = conn.execute(
            "SELECT COUNT(*) FROM invalid_usernames WHERE username=?", (username,)
        )
        count = cur.fetchone()[0]
        conn.close()
        assert count == 0

    def test_clear_nonexistent_username_does_not_raise(self, tracker):
        tracker.clear_username("nonexistent_xyz")  # must not raise

    def test_clear_only_removes_target_username(self, tracker):
        tracker.record_invalid("keep_me", InvalidReason.NOT_FOUND)
        tracker.record_invalid("remove_me", InvalidReason.NOT_FOUND)
        tracker.clear_username("remove_me")

        result = tracker.get_invalid_usernames()
        assert "keep_me" in result
        assert "remove_me" not in result


# ── Property 20: Detailed Records Retrieval ───────────────────────────────────

class TestDetailedRecordsRetrieval:
    """Property 20: get_invalid_records() returns complete InvalidUsernameRecord objects.

    Validates: Requirements 10.5
    """

    @given(usernames=_usernames_st, reason=_reason_st)
    @settings(max_examples=20)
    def test_get_invalid_records_returns_all_fields(self, usernames, reason):
        tracker = _make_tracker()
        for u in usernames:
            tracker.record_invalid(u, reason, error_message=f"err_{u}")

        records = tracker.get_invalid_records()
        assert len(records) == len(usernames)

        for record in records:
            assert isinstance(record, InvalidUsernameRecord)
            assert record.username in usernames
            assert isinstance(record.reason, InvalidReason)
            assert record.detected_at > 0
            assert record.error_message is not None
            assert isinstance(record.retry_count, int)

    def test_empty_tracker_returns_empty_list(self, tracker):
        assert tracker.get_invalid_records() == []

    def test_records_have_correct_reason(self, tracker):
        tracker.record_invalid("user1", InvalidReason.ACCOUNT_DELETED)
        tracker.record_invalid("user2", InvalidReason.USERNAME_CHANGED)

        records = tracker.get_invalid_records()
        by_username = {r.username: r for r in records}

        assert by_username["user1"].reason == InvalidReason.ACCOUNT_DELETED
        assert by_username["user2"].reason == InvalidReason.USERNAME_CHANGED
