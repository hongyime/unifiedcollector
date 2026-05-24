"""Tests for priority_manager.py — relationship-based prioritisation.

PriorityManager._load_relationships() now uses RelationshipRepository (DB-backed).
Fixtures mock the repository so no real DB or files are needed.
"""
from unittest.mock import patch, MagicMock

import pytest

from priority_manager import PriorityManager


def _mock_tracker():
    mock_tracker = MagicMock()
    mock_tracker.get_access_statistics.return_value = {}
    mock_tracker.get_profile_summary.return_value = {"is_public": False}
    return mock_tracker


# ── Fixture ──────────────────────────────────────────────────

@pytest.fixture
def pm_with_rels(sample_relationships):
    """PriorityManager loaded with sample_relationships; DB calls mocked out."""
    with patch.object(PriorityManager, "_load_relationships", return_value=sample_relationships), \
         patch("priority_manager.ProfileAccessTracker", return_value=_mock_tracker()):
        return PriorityManager()


# ══════════════════════════════════════════════════════════════

class TestGetAccountConnections:

    def test_finds_followers(self, pm_with_rels):
        conns = pm_with_rels.get_account_connections("user_one")
        assert "alice" in conns["followers"]
        assert "charlie" in conns["followers"]

    def test_finds_following(self, pm_with_rels):
        conns = pm_with_rels.get_account_connections("user_one")
        assert "bob" in conns["following"]
        assert "diana" in conns["following"]

    def test_mutual_in_both(self, pm_with_rels):
        conns = pm_with_rels.get_account_connections("user_one")
        # charlie has both a followers and following entry
        assert "charlie" in conns["followers"]
        assert "charlie" in conns["following"]

    def test_unknown_account_empty(self, pm_with_rels):
        conns = pm_with_rels.get_account_connections("nobody")
        assert len(conns["followers"]) == 0
        assert len(conns["following"]) == 0


class TestPrioritizeUsernames:

    def test_mutual_detected(self, pm_with_rels):
        cats = pm_with_rels.prioritize_usernames(
            ["alice", "bob", "charlie", "diana", "eve"], "user_one"
        )
        assert "charlie" in cats["mutual_connections"]

    def test_followers_only(self, pm_with_rels):
        cats = pm_with_rels.prioritize_usernames(
            ["alice", "bob", "charlie"], "user_one"
        )
        assert "alice" in cats["followers_only"]

    def test_following_only(self, pm_with_rels):
        cats = pm_with_rels.prioritize_usernames(
            ["alice", "bob", "charlie", "diana"], "user_one"
        )
        assert "bob" in cats["following_only"]
        assert "diana" in cats["following_only"]

    def test_unknown_goes_to_last_bucket(self, pm_with_rels):
        cats = pm_with_rels.prioritize_usernames(["eve"], "user_one")
        assert "eve" in cats["unknown_private"]


class TestGetPrioritizedList:

    def test_order_mutual_first(self, pm_with_rels):
        ordered = pm_with_rels.get_prioritized_list(
            ["eve", "alice", "charlie", "bob"], "user_one"
        )
        # charlie (mutual) should come before alice (follower)
        assert ordered.index("charlie") < ordered.index("alice")
        # alice (follower) before bob (following)
        assert ordered.index("alice") < ordered.index("bob")
        # bob (following) before eve (unknown)
        assert ordered.index("bob") < ordered.index("eve")


class TestGetHighPriorityUsers:

    def test_excludes_unknown(self, pm_with_rels):
        high = pm_with_rels.get_high_priority_users(
            ["alice", "bob", "charlie", "eve"], "user_one"
        )
        assert "eve" not in high

    def test_respects_max_users(self, pm_with_rels):
        high = pm_with_rels.get_high_priority_users(
            ["alice", "bob", "charlie", "diana"], "user_one", max_users=2
        )
        assert len(high) == 2


class TestCategoryStats:

    def test_returns_all_categories(self, pm_with_rels):
        stats = pm_with_rels.get_category_stats(
            ["alice", "bob", "charlie", "eve"], "user_one"
        )
        for key in ("mutual_connections", "followers_only", "following_only",
                     "public_accessible", "unknown_private"):
            assert key in stats
            assert "count" in stats[key]
            assert "percentage" in stats[key]

    def test_empty_username_list_no_division_error(self, pm_with_rels):
        stats = pm_with_rels.get_category_stats([], "user_one")
        for key in ("mutual_connections", "followers_only", "following_only",
                     "public_accessible", "unknown_private"):
            assert stats[key]["count"] == 0
            assert stats[key]["percentage"] == 0


class TestPrintPrioritizationSummary:

    def test_summary_with_all_zeroes(self, pm_with_rels, capsys):
        """When no high-priority users exist, the WARNING line should appear."""
        pm_with_rels.prioritize_usernames(["eve"], "user_one")
        output = capsys.readouterr().out
        assert "WARNING" in output

    def test_summary_with_high_priority(self, pm_with_rels, capsys):
        pm_with_rels.prioritize_usernames(
            ["alice", "bob", "charlie"], "user_one"
        )
        output = capsys.readouterr().out
        assert "SUCCESS" in output


class TestLoadRelationshipsEdgeCases:

    def test_db_error_returns_empty(self):
        """When DB repo raises inside _load_relationships, relationships falls back to []."""
        # Patch the inner import inside _load_relationships so it raises
        with patch("db.repositories.relationship_repository.RelationshipRepository",
                   side_effect=Exception("db down")), \
             patch("priority_manager._get_db"), \
             patch("priority_manager.ProfileAccessTracker", return_value=_mock_tracker()):
            pm = PriorityManager()
        assert pm.relationships == []

    def test_empty_db_returns_empty(self):
        """Zero rows → empty relationships list."""
        with patch.object(PriorityManager, "_load_relationships", return_value=[]), \
             patch("priority_manager.ProfileAccessTracker", return_value=_mock_tracker()):
            pm = PriorityManager()
        assert pm.relationships == []


class TestGetHighPriorityEdgeCases:

    def test_no_high_priority_returns_empty(self, pm_with_rels):
        """All unknowns should yield empty high-priority list."""
        high = pm_with_rels.get_high_priority_users(["eve", "frank"], "user_one")
        assert high == []

    def test_max_users_none_returns_all(self, pm_with_rels):
        high = pm_with_rels.get_high_priority_users(
            ["alice", "bob", "charlie", "diana"], "user_one", max_users=None
        )
        # Should have all high-priority (alice follower, bob following,
        # charlie mutual, diana following) = 4
        assert len(high) == 4
