"""
Unit tests for SmartAccountSelector.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.7
"""

from unittest.mock import MagicMock

import pytest

from src.operation_classifier import OperationType
from src.smart_account_selector import SmartAccountSelector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(source_account="account1", following_status=None):
    record = MagicMock()
    record.source_account = source_account
    record.following_status = following_status or {}
    return record


def _make_db(record=None):
    db = MagicMock()
    db.get_username_record.return_value = record
    return db


def _make_tracker(accessible_by=None):
    tracker = MagicMock()
    tracker.get_profile_summary.return_value = {
        "accessible_by": accessible_by or [],
        "status": "tracked",
    }
    return tracker


# ---------------------------------------------------------------------------
# select_for_operation — PUBLIC
# ---------------------------------------------------------------------------

class TestPublicOperationSelection:
    """Requirement 3.1: PUBLIC operations return any available account."""

    def test_returns_first_available_account(self):
        selector = SmartAccountSelector()
        result = selector.select_for_operation(
            OperationType.PUBLIC, "user1", ["account1", "account2"]
        )
        assert result == "account1"

    def test_returns_single_account_when_only_one(self):
        selector = SmartAccountSelector()
        result = selector.select_for_operation(
            OperationType.PUBLIC, "user1", ["account1"]
        )
        assert result == "account1"

    def test_returns_none_when_no_accounts(self):
        selector = SmartAccountSelector()
        result = selector.select_for_operation(OperationType.PUBLIC, "user1", [])
        assert result is None


# ---------------------------------------------------------------------------
# select_for_operation — FOLLOWING_REQUIRED
# ---------------------------------------------------------------------------

class TestFollowingRequiredSelection:
    """Requirements 3.2, 3.3, 3.4: FOLLOWING_REQUIRED uses following relationships."""

    def test_returns_account_from_cache(self):
        """Requirement 3.2: Returns account that follows target (from cache)."""
        record = _make_record(following_status={"account2": True, "account1": False})
        db = _make_db(record)
        selector = SmartAccountSelector(username_db=db)

        result = selector.select_for_operation(
            OperationType.FOLLOWING_REQUIRED, "user1", ["account1", "account2"]
        )
        assert result == "account2"

    def test_returns_account_from_tracker(self):
        """Requirement 3.6: Falls back to ProfileAccessTracker when cache misses."""
        record = _make_record(following_status={})
        db = _make_db(record)
        tracker = _make_tracker(accessible_by=["account2"])
        selector = SmartAccountSelector(username_db=db, profile_tracker=tracker)

        result = selector.select_for_operation(
            OperationType.FOLLOWING_REQUIRED, "user1", ["account1", "account2"]
        )
        assert result == "account2"

    def test_falls_back_to_source_account(self):
        """Requirement 3.3: Falls back to source account when no following found."""
        record = _make_record(source_account="account1", following_status={})
        db = _make_db(record)
        tracker = _make_tracker(accessible_by=[])
        selector = SmartAccountSelector(username_db=db, profile_tracker=tracker)

        result = selector.select_for_operation(
            OperationType.FOLLOWING_REQUIRED, "user1", ["account1", "account2"]
        )
        assert result == "account1"

    def test_returns_none_when_no_following_relationship(self):
        """Requirement 3.4: Returns None when no following relationship found."""
        record = _make_record(source_account="account3", following_status={})
        db = _make_db(record)
        tracker = _make_tracker(accessible_by=[])
        selector = SmartAccountSelector(username_db=db, profile_tracker=tracker)

        # source_account not in available_accounts
        result = selector.select_for_operation(
            OperationType.FOLLOWING_REQUIRED, "user1", ["account1", "account2"]
        )
        assert result is None

    def test_returns_none_when_no_accounts_available(self):
        selector = SmartAccountSelector()
        result = selector.select_for_operation(
            OperationType.FOLLOWING_REQUIRED, "user1", []
        )
        assert result is None

    def test_cache_takes_priority_over_tracker(self):
        """Cache is checked before ProfileAccessTracker."""
        record = _make_record(following_status={"account1": True})
        db = _make_db(record)
        tracker = _make_tracker(accessible_by=["account2"])
        selector = SmartAccountSelector(username_db=db, profile_tracker=tracker)

        result = selector.select_for_operation(
            OperationType.FOLLOWING_REQUIRED, "user1", ["account1", "account2"]
        )
        assert result == "account1"
        # Tracker should NOT have been called since cache hit
        tracker.get_profile_summary.assert_not_called()


# ---------------------------------------------------------------------------
# Cache update after tracker query
# ---------------------------------------------------------------------------

class TestCacheUpdateAfterTrackerQuery:
    """Requirement 3.7: Following status from tracker updates cache."""

    def test_cache_updated_when_tracker_finds_account(self):
        record = _make_record(following_status={})
        db = _make_db(record)
        tracker = _make_tracker(accessible_by=["account2"])
        selector = SmartAccountSelector(username_db=db, profile_tracker=tracker)

        selector.select_for_operation(
            OperationType.FOLLOWING_REQUIRED, "user1", ["account1", "account2"]
        )

        db.update_metadata.assert_called_once()
        call_args = db.update_metadata.call_args[0]
        assert call_args[0] == "user1"
        assert call_args[1]["following_status"]["account2"] is True

    def test_cache_not_updated_when_no_record(self):
        """No crash when username_db returns None for record."""
        db = _make_db(record=None)
        tracker = _make_tracker(accessible_by=["account2"])
        selector = SmartAccountSelector(username_db=db, profile_tracker=tracker)

        result = selector.select_for_operation(
            OperationType.FOLLOWING_REQUIRED, "user1", ["account1", "account2"]
        )
        # Should still return the account from tracker
        assert result == "account2"
        # But no cache update since record is None
        db.update_metadata.assert_not_called()


# ---------------------------------------------------------------------------
# select_for_batch
# ---------------------------------------------------------------------------

class TestBatchGroupingOptimization:
    """Requirements 3.5, 7.2, 7.3: Batch processing groups by optimal account."""

    def test_public_batch_assigns_all_to_single_account(self):
        """Requirement 7.2: PUBLIC batch assigns all usernames to one account."""
        selector = SmartAccountSelector()
        assignment = selector.select_for_batch(
            OperationType.PUBLIC,
            ["user1", "user2", "user3"],
            ["account1", "account2"],
        )
        assert len(assignment) == 1
        assert "account1" in assignment
        assert sorted(assignment["account1"]) == ["user1", "user2", "user3"]

    def test_following_required_batch_groups_by_following(self):
        """Requirement 7.3: FOLLOWING_REQUIRED groups by following relationships."""
        def get_record(username):
            record = MagicMock()
            if username in ("user1", "user3"):
                record.following_status = {"account1": True}
                record.source_account = "account1"
            else:
                record.following_status = {"account2": True}
                record.source_account = "account2"
            return record

        db = MagicMock()
        db.get_username_record.side_effect = get_record
        selector = SmartAccountSelector(username_db=db)

        assignment = selector.select_for_batch(
            OperationType.FOLLOWING_REQUIRED,
            ["user1", "user2", "user3"],
            ["account1", "account2"],
        )

        assert sorted(assignment.get("account1", [])) == ["user1", "user3"]
        assert assignment.get("account2", []) == ["user2"]

    def test_batch_returns_empty_for_empty_inputs(self):
        selector = SmartAccountSelector()
        assert selector.select_for_batch(OperationType.PUBLIC, [], ["account1"]) == {}
        assert selector.select_for_batch(OperationType.PUBLIC, ["user1"], []) == {}

    def test_batch_all_usernames_covered_when_no_following(self):
        """All usernames assigned even when no following relationship found."""
        record = _make_record(source_account="account3", following_status={})
        db = _make_db(record)
        selector = SmartAccountSelector(username_db=db)

        assignment = selector.select_for_batch(
            OperationType.FOLLOWING_REQUIRED,
            ["user1", "user2"],
            ["account1"],
        )

        all_assigned = []
        for lst in assignment.values():
            all_assigned.extend(lst)
        assert sorted(all_assigned) == ["user1", "user2"]


# ---------------------------------------------------------------------------
# get_following_overlap
# ---------------------------------------------------------------------------

class TestGetFollowingOverlap:
    """Tests for get_following_overlap() helper."""

    def test_returns_true_for_cached_following(self):
        def get_record(username):
            record = MagicMock()
            record.following_status = {"account1": True}
            return record

        db = MagicMock()
        db.get_username_record.side_effect = get_record
        selector = SmartAccountSelector(username_db=db)

        result = selector.get_following_overlap("account1", ["user1", "user2"])
        assert result == {"user1": True, "user2": True}

    def test_returns_false_when_not_following(self):
        record = _make_record(following_status={"account1": False})
        db = _make_db(record)
        selector = SmartAccountSelector(username_db=db)

        result = selector.get_following_overlap("account1", ["user1"])
        assert result == {"user1": False}

    def test_returns_false_when_no_data(self):
        db = _make_db(record=None)
        selector = SmartAccountSelector(username_db=db)

        result = selector.get_following_overlap("account1", ["user1"])
        assert result == {"user1": False}

    def test_empty_username_list_returns_empty_dict(self):
        selector = SmartAccountSelector()
        result = selector.get_following_overlap("account1", [])
        assert result == {}
