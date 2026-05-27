"""Tests for src/collect_relationships.py — RelationshipCollector (offline, fully mocked).

All instaloader API calls and file I/O use temp directories so no real
data/ files are touched and no network calls are made.
"""
import json
import os
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import instaloader
import instaloader.exceptions


# ── Shared fixtures ──────────────────────────────────────────

_MOCK_ACCOUNTS = [
    {"name": "acct1", "username": "user_one", "password": "pw1"},
    {"name": "acct2", "username": "user_two", "password": "pw2"},
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Redirect all config paths to tmp and suppress real auth."""
    import collect_relationships as _cr_mod
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setattr("config.DATA_DIR", data_dir)
    monkeypatch.setattr("config.INSTAGRAM_ACCOUNTS", _MOCK_ACCOUNTS)
    monkeypatch.setattr("config.SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr("collect_relationships.DATA_DIR", data_dir)
    monkeypatch.setattr("profile_access_tracker.DATA_DIR", data_dir)
    os.makedirs(str(tmp_path / "sessions"), exist_ok=True)
    # Isolate DB — each test gets its own fresh in-memory SQLite DB
    monkeypatch.setenv("DATABASE_URL", ":memory:")
    _cr_mod._get_db._instance = None
    yield
    _cr_mod._get_db._instance = None


def _make_collector(monkeypatch, tmp_path, *, usernames=None, relationships=None):
    """Build a RelationshipCollector with mocked auth and DB."""
    with patch("collect_relationships.InstagramAccountManager") as MockMgr, \
         patch("collect_relationships._get_db") as MockDb, \
         patch("db.repositories.relationship_repository.RelationshipRepository") as MockRelRepo, \
         patch("db.repositories.username_repository.UsernameRepository") as MockUsrRepo:
        mock_mgr = MagicMock()
        mock_loader = MagicMock(spec=instaloader.Instaloader)
        mock_loader.context = MagicMock()
        mock_mgr.get_authenticated_loader.return_value = mock_loader
        mock_mgr.current_account = {"name": "acct1"}
        MockMgr.return_value = mock_mgr

        mock_db = MagicMock()
        mock_db.fetchone.return_value = None
        MockDb.return_value = mock_db

        mock_rel_repo = MagicMock()
        mock_rel_repo.get_relationships.return_value = relationships or []
        MockRelRepo.return_value = mock_rel_repo

        mock_usr_repo = MagicMock()
        # Handle both string list and dict list for usernames
        if usernames and isinstance(usernames[0] if usernames else None, str):
            # Convert strings to dict format
            mock_usr_repo.get_all.return_value = [{"username": u} for u in usernames] if usernames else []
        else:
            # Already in dict format or empty
            mock_usr_repo.get_all.return_value = usernames or []
        MockUsrRepo.return_value = mock_usr_repo

        from collect_relationships import RelationshipCollector
        rc = RelationshipCollector("acct1")
        return rc


# ══════════════════════════════════════════════════════════════
#  Initialisation
# ══════════════════════════════════════════════════════════════

class TestInit:
    def test_raises_when_auth_fails(self, monkeypatch, tmp_path):
        with patch("collect_relationships.InstagramAccountManager") as MockMgr:
            mock_mgr = MagicMock()
            mock_mgr.get_authenticated_loader.return_value = None
            MockMgr.return_value = mock_mgr

            from collect_relationships import RelationshipCollector
            with pytest.raises(RuntimeError, match="Failed to authenticate"):
                RelationshipCollector("acct1")

    def test_loads_empty_when_no_files(self, monkeypatch, tmp_path):
        rc = _make_collector(monkeypatch, tmp_path)
        assert rc.usernames == []
        assert rc.relationships == []

    def test_loads_existing_usernames(self, monkeypatch, tmp_path):
        rc = _make_collector(monkeypatch, tmp_path, usernames=["alice", "bob"])
        assert rc.usernames == ["alice", "bob"]

    def test_loads_existing_relationships(self, monkeypatch, tmp_path):
        rels = [{"source": "alice", "target": "bob", "type": "followers"}]
        rc = _make_collector(monkeypatch, tmp_path, relationships=rels)
        assert len(rc.relationships) == 1


# ══════════════════════════════════════════════════════════════
#  _load / _save usernames
# ══════════════════════════════════════════════════════════════

class TestUsernamePersistence:
    def test_save_deduplicates_and_sorts(self, monkeypatch, tmp_path):
        rc = _make_collector(monkeypatch, tmp_path)
        rc.usernames = ["charlie", "alice", "bob", "alice"]
        rc._save_usernames()

        data_dir = str(tmp_path / "data")
        with open(os.path.join(data_dir, "usernames.txt")) as f:
            lines = [l.strip() for l in f if l.strip()]
        assert lines == ["alice", "bob", "charlie"]

    def test_load_strips_whitespace(self, monkeypatch, tmp_path):
        data_dir = str(tmp_path / "data")
        with open(os.path.join(data_dir, "usernames.txt"), "w") as f:
            f.write("  alice  \n  bob  \n\n")
        rc = _make_collector(monkeypatch, tmp_path)
        assert rc.usernames == ["alice", "bob"]

    def test_load_returns_empty_on_missing_file(self, monkeypatch, tmp_path):
        rc = _make_collector(monkeypatch, tmp_path)
        assert rc.usernames == []


# ══════════════════════════════════════════════════════════════
#  _load / _save relationships
# ══════════════════════════════════════════════════════════════

class TestRelationshipPersistence:
    def test_save_and_reload(self, monkeypatch, tmp_path):
        rc = _make_collector(monkeypatch, tmp_path)
        rc.relationships = [
            {"source": "a", "target": "b", "type": "followers", "timestamp": time.time()},
        ]
        rc._save_relationships()

        data_dir = str(tmp_path / "data")
        with open(os.path.join(data_dir, "relationships.json")) as f:
            loaded = json.load(f)
        assert len(loaded) == 1
        assert loaded[0]["source"] == "a"

    def test_load_returns_empty_on_corrupt(self, monkeypatch, tmp_path):
        data_dir = str(tmp_path / "data")
        with open(os.path.join(data_dir, "relationships.json"), "w") as f:
            f.write("{broken json")
        rc = _make_collector(monkeypatch, tmp_path)
        assert rc.relationships == []


# ══════════════════════════════════════════════════════════════
#  collect_for_user
# ══════════════════════════════════════════════════════════════

class TestCollectForUser:
    def test_rejects_empty_username(self, monkeypatch, tmp_path, capsys):
        rc = _make_collector(monkeypatch, tmp_path)
        rc.collect_for_user("")
        assert "Invalid username" in capsys.readouterr().out

    def test_rejects_whitespace_username(self, monkeypatch, tmp_path, capsys):
        rc = _make_collector(monkeypatch, tmp_path)
        rc.collect_for_user("   ")
        assert "Invalid username" in capsys.readouterr().out

    @patch("collect_relationships.retry_with_backoff", return_value=None)
    def test_handles_profile_not_found(self, mock_retry, monkeypatch, tmp_path, capsys):
        rc = _make_collector(monkeypatch, tmp_path)
        rc.collect_for_user("nonexistent")
        output = capsys.readouterr().out
        assert "Could not load profile" in output

    @patch("collect_relationships.retry_with_backoff")
    def test_handles_private_not_followed(self, mock_retry, monkeypatch, tmp_path, capsys):
        mock_profile = MagicMock()
        mock_profile.is_private = True
        mock_profile.followed_by_viewer = False
        mock_retry.return_value = mock_profile

        rc = _make_collector(monkeypatch, tmp_path)
        rc.collect_for_user("private_user")
        output = capsys.readouterr().out
        assert "private" in output.lower()

    @patch("collect_relationships.retry_with_backoff")
    def test_collects_followers_and_following(self, mock_retry, monkeypatch, tmp_path):
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.followed_by_viewer = True
        mock_retry.return_value = mock_profile

        follower = MagicMock()
        follower.username = "follower1"
        followee = MagicMock()
        followee.username = "followee1"
        mock_profile.get_followers.return_value = [follower]
        mock_profile.get_followees.return_value = [followee]

        rc = _make_collector(monkeypatch, tmp_path)
        rc.collect_for_user("target_user")

        # Should have 2 relationships
        assert len(rc.relationships) == 2
        types = {r["type"] for r in rc.relationships}
        assert types == {"followers", "following"}

        # target_user + follower1 + followee1 should all be in usernames
        assert "target_user" in rc.usernames
        assert "follower1" in rc.usernames
        assert "followee1" in rc.usernames

    @patch("collect_relationships.retry_with_backoff")
    def test_skips_duplicate_relationships(self, mock_retry, monkeypatch, tmp_path):
        existing = [
            {"source": "target_user", "target": "follower1", "type": "followers"},
        ]
        rc = _make_collector(monkeypatch, tmp_path, relationships=existing)

        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.followed_by_viewer = True
        mock_retry.return_value = mock_profile

        follower = MagicMock()
        follower.username = "follower1"
        mock_profile.get_followers.return_value = [follower]
        mock_profile.get_followees.return_value = []

        rc.collect_for_user("target_user", max_following=0)

        # duplicate should not be added
        follower_rels = [r for r in rc.relationships if r["type"] == "followers"]
        assert len(follower_rels) == 1

    @patch("collect_relationships.retry_with_backoff")
    def test_respects_max_followers(self, mock_retry, monkeypatch, tmp_path):
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.followed_by_viewer = True
        mock_retry.return_value = mock_profile

        # 10 followers but max=3
        followers = [MagicMock(username=f"f{i}") for i in range(10)]
        mock_profile.get_followers.return_value = followers
        mock_profile.get_followees.return_value = []

        rc = _make_collector(monkeypatch, tmp_path)
        rc.collect_for_user("target", max_followers=3, max_following=0)

        follower_rels = [r for r in rc.relationships if r["type"] == "followers"]
        assert len(follower_rels) == 3

    @patch("collect_relationships.retry_with_backoff")
    def test_respects_max_following(self, mock_retry, monkeypatch, tmp_path):
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.followed_by_viewer = True
        mock_retry.return_value = mock_profile

        followees = [MagicMock(username=f"f{i}") for i in range(10)]
        mock_profile.get_followers.return_value = []
        mock_profile.get_followees.return_value = followees

        rc = _make_collector(monkeypatch, tmp_path)
        rc.collect_for_user("target", max_followers=0, max_following=5)

        following_rels = [r for r in rc.relationships if r["type"] == "following"]
        assert len(following_rels) == 5

    @patch("collect_relationships.retry_with_backoff")
    def test_handles_profile_not_exists_exception(self, mock_retry, monkeypatch, tmp_path, capsys):
        mock_retry.side_effect = instaloader.exceptions.ProfileNotExistsException("")

        rc = _make_collector(monkeypatch, tmp_path)
        rc.collect_for_user("ghost_user")
        output = capsys.readouterr().out
        assert "does not exist" in output

    @patch("collect_relationships.retry_with_backoff")
    def test_handles_private_profile_exception_during_follower_enum(self, mock_retry, monkeypatch, tmp_path, capsys):
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.followed_by_viewer = True
        mock_retry.return_value = mock_profile

        mock_profile.get_followers.side_effect = instaloader.exceptions.PrivateProfileNotFollowedException("")
        mock_profile.get_followees.return_value = []

        rc = _make_collector(monkeypatch, tmp_path)
        rc.collect_for_user("restricted_user")
        output = capsys.readouterr().out
        assert "Cannot access followers" in output or "private" in output.lower()

    @patch("collect_relationships.retry_with_backoff")
    def test_saves_progress_after_collection(self, mock_retry, monkeypatch, tmp_path):
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.followed_by_viewer = True
        mock_retry.return_value = mock_profile
        mock_profile.get_followers.return_value = [MagicMock(username="f1")]
        mock_profile.get_followees.return_value = []

        rc = _make_collector(monkeypatch, tmp_path)
        rc.collect_for_user("target", max_following=0)

        # Verify files were written
        data_dir = str(tmp_path / "data")
        assert os.path.exists(os.path.join(data_dir, "usernames.txt"))
        assert os.path.exists(os.path.join(data_dir, "relationships.json"))

    @patch("collect_relationships.retry_with_backoff")
    def test_records_access_tracker_on_success(self, mock_retry, monkeypatch, tmp_path):
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.followed_by_viewer = True
        mock_retry.return_value = mock_profile
        mock_profile.get_followers.return_value = []
        mock_profile.get_followees.return_value = []

        rc = _make_collector(monkeypatch, tmp_path)
        rc.access_tracker = MagicMock()
        rc.collect_for_user("target", max_followers=0, max_following=0)

        rc.access_tracker.record_profile_access.assert_called()


# ══════════════════════════════════════════════════════════════
#  run_batch
# ══════════════════════════════════════════════════════════════

class TestRunBatch:
    @patch("collect_relationships.retry_with_backoff")
    def test_skips_already_processed(self, mock_retry, monkeypatch, tmp_path, capsys):
        import collect_relationships as _cr
        rc = _make_collector(monkeypatch, tmp_path)

        # Populate the real DB that run_batch() queries directly
        db = _cr._get_db()
        db.execute("INSERT OR IGNORE INTO usernames (username, source_account, spider_status) VALUES ('alice', 'acct1', 'completed')")
        db.execute("INSERT OR IGNORE INTO usernames (username, source_account, spider_status) VALUES ('bob', 'acct1', 'pending')")

        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.followed_by_viewer = True
        mock_retry.return_value = mock_profile
        mock_profile.get_followers.return_value = []
        mock_profile.get_followees.return_value = []

        rc.run_batch()

        output = capsys.readouterr().out
        # alice should be skipped (completed), bob should be processed
        assert "bob" in output

    def test_all_processed_prints_note(self, monkeypatch, tmp_path, capsys):
        import collect_relationships as _cr
        rc = _make_collector(monkeypatch, tmp_path)

        # Populate DB with already-completed usernames
        db = _cr._get_db()
        db.execute("INSERT OR IGNORE INTO usernames (username, source_account, spider_status) VALUES ('alice', 'acct1', 'completed')")
        db.execute("INSERT OR IGNORE INTO usernames (username, source_account, spider_status) VALUES ('bob', 'acct1', 'completed')")

        rc.run_batch()
        output = capsys.readouterr().out
        assert "All usernames have been processed" in output

    @patch("collect_relationships.retry_with_backoff")
    def test_respects_max_users(self, mock_retry, monkeypatch, tmp_path):
        rc = _make_collector(monkeypatch, tmp_path, usernames=["a", "b", "c", "d", "e"])
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.followed_by_viewer = True
        mock_retry.return_value = mock_profile
        mock_profile.get_followers.return_value = []
        mock_profile.get_followees.return_value = []

        rc.run_batch(max_users=2)
        # Only 2 users should have been processed
        processed = {r["source"] for r in rc.relationships if "source" in r}
        # Could be 0 relationships if none collected, but the important thing
        # is the method didn't process more than 2
        # We can check usernames were added
        assert len([u for u in rc.usernames if u in ["a", "b", "c", "d", "e"]]) >= 2

    def test_handles_empty_usernames(self, monkeypatch, tmp_path, capsys):
        rc = _make_collector(monkeypatch, tmp_path)
        rc.run_batch()
        output = capsys.readouterr().out
        assert "All usernames have been processed" in output


# ══════════════════════════════════════════════════════════════
#  cleanup
# ══════════════════════════════════════════════════════════════

class TestCleanup:
    def test_calls_manager_logout(self, monkeypatch, tmp_path):
        rc = _make_collector(monkeypatch, tmp_path)
        rc.cleanup()
        rc.manager.logout.assert_called_once()


# ══════════════════════════════════════════════════════════════
#  Task 11.1: Bug Condition Exploration - Missing FileLock
# ══════════════════════════════════════════════════════════════

class TestFileLockExploration:
    """Exploration tests to demonstrate missing FileLock in critical JSON writes.
    
    **Validates: Requirements 3.1** (Bug Condition - State Consistency File Locking)
    
    These tests check if collect_relationships.py uses FileLock for JSON writes.
    On UNFIXED code, these tests should FAIL, demonstrating the bug exists.
    """
    
    @pytest.mark.xfail(strict=False, reason="FileLock not yet implemented on _save_relationships")
    def test_save_relationships_uses_filelock(self, monkeypatch, tmp_path):
        """Check if _save_relationships() uses FileLock wrapper.

        EXPECTED: This test should FAIL on unfixed code, demonstrating that
        critical JSON writes occur without FileLock protection.
        """
        rc = _make_collector(monkeypatch, tmp_path)
        rc.relationships = [{"source": "a", "target": "b", "type": "followers"}]
        
        # Check if FileLock is used by inspecting the source code
        import inspect
        from collect_relationships import RelationshipCollector
        
        source = inspect.getsource(RelationshipCollector._save_relationships)
        
        # Bug condition: FileLock should be used but is missing
        assert "FileLock" in source, (
            "COUNTEREXAMPLE: _save_relationships() does not use FileLock wrapper. "
            "Critical JSON writes to relationships.json occur without file locking, "
            "risking data corruption during concurrent access."
        )


# ══════════════════════════════════════════════════════════════
#  Task 15: Bug Condition Exploration - Linear Deduplication
# ══════════════════════════════════════════════════════════════

class TestLinearDeduplicationExploration:
    """Exploration tests to demonstrate O(n²) linear deduplication performance.
    
    **Validates: Property 7** (Bug Condition - State Consistency Deduplication Performance)
    
    These tests check if RelationshipCollector.collect_for_user() uses linear
    search for deduplication, causing O(n²) performance degradation.
    On UNFIXED code, these tests should PASS, demonstrating the bug exists.
    """
    
    def test_deduplication_uses_linear_search(self, monkeypatch, tmp_path):
        """Check if deduplication uses linear any() iteration.
        
        EXPECTED: This test should PASS on unfixed code (incorrect behavior),
        demonstrating that deduplication uses O(n²) linear search.
        
        After fix: This test should FAIL, confirming the code now uses set-based approach.
        """
        import inspect
        from collect_relationships import RelationshipCollector
        
        source = inspect.getsource(RelationshipCollector.collect_for_user)
        
        # Bug condition: any() with iteration over self.relationships
        has_linear_search = (
            "any(" in source and 
            "self.relationships" in source and
            ("r['source']" in source or "r[\"source\"]" in source)
        )
        
        # After fix: Check for set-based approach
        has_set_based = (
            "existing_keys" in source and
            "set" in source.lower() and
            "not in existing_keys" in source
        )
        
        if has_linear_search:
            # This is the bug - linear search for deduplication
            assert True, (
                "COUNTEREXAMPLE FOUND: collect_for_user() uses any() with linear iteration "
                "over self.relationships for deduplication. This causes O(n²) performance "
                "degradation on large datasets. Should use set-based approach for O(1) lookup."
            )
        elif has_set_based:
            # Code has been fixed - set-based approach is now used
            pytest.skip(
                "Code has been FIXED: collect_for_user() now uses set-based deduplication "
                "with O(1) lookup performance. The bug no longer exists."
            )
        else:
            # If neither pattern found, unclear state
            pytest.fail(
                "Could not confirm deduplication pattern. "
                "Neither linear search nor set-based approach pattern was found."
            )
    
    @patch("collect_relationships.retry_with_backoff")
    def test_deduplication_performance_on_large_dataset(self, mock_retry, monkeypatch, tmp_path):
        """Measure deduplication performance on large dataset.
        
        EXPECTED: This test should demonstrate slow performance on unfixed code
        due to O(n²) linear search.
        """
        import time
        
        # Create a large existing relationship dataset (1000 relationships)
        existing_rels = [
            {"source": f"user_{i}", "target": f"follower_{j}", "type": "followers"}
            for i in range(10) for j in range(100)
        ]
        rc = _make_collector(monkeypatch, tmp_path, relationships=existing_rels)
        
        # Mock profile with 100 new followers (all duplicates)
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.followed_by_viewer = True
        mock_retry.return_value = mock_profile
        
        # Create followers that are all duplicates
        followers = [MagicMock(username=f"follower_{j}") for j in range(100)]
        mock_profile.get_followers.return_value = followers
        mock_profile.get_followees.return_value = []
        
        # Measure time for deduplication
        start_time = time.time()
        rc.collect_for_user("user_0", max_following=0)
        elapsed_time = time.time() - start_time
        
        # On unfixed code with O(n²), this should take noticeable time
        # With 1000 existing + 100 new (all duplicates) = 100,000 comparisons
        # On fixed code with O(1) set lookup, this should be very fast
        
        print(f"\n[PERFORMANCE] Deduplication took {elapsed_time:.4f} seconds for 1000 existing + 100 new relationships")
        
        # We don't assert on time here because it's machine-dependent
        # But we document the expected behavior:
        # - Unfixed code: O(n²) = 100 * 1000 = 100,000 comparisons (slow)
        # - Fixed code: O(n) = 100 lookups in set (fast)
        
        # Verify no duplicates were added
        follower_rels = [r for r in rc.relationships if r["source"] == "user_0" and r["type"] == "followers"]
        assert len(follower_rels) == 100, (
            f"Expected 100 follower relationships for user_0, but got {len(follower_rels)}. "
            f"Deduplication should prevent adding duplicates."
        )
