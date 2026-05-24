"""Unit tests for ProfileAccessRepository using in-memory SQLite."""
import sys
import os
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from db.manager import DatabaseManager
from db.repositories.profile_access_repository import ProfileAccessRepository


@pytest.fixture
def db():
    manager = DatabaseManager("sqlite:///:memory:")
    yield manager
    manager.close()


@pytest.fixture
def repo(db):
    return ProfileAccessRepository(db)


class TestProfileAccessRepositoryCRUD:
    def test_record_attempt_inserts_row(self, repo, db):
        repo.record_attempt("alice", "acct1", True, True, False)
        rows = db.fetchall("SELECT * FROM profile_access_attempts WHERE target_username='alice'")
        assert len(rows) == 1

    def test_record_attempt_upserts_summary(self, repo, db):
        repo.record_attempt("alice", "acct1", True, True, False)
        summary = db.fetchone("SELECT * FROM profile_access_summary WHERE username='alice'")
        assert summary is not None
        assert summary["total_attempts"] == 1

    def test_record_attempt_increments_total(self, repo):
        repo.record_attempt("alice", "acct1", True, True, False)
        repo.record_attempt("alice", "acct2", False, True, False)
        summary = repo.get_profile_summary("alice")
        assert summary["total_attempts"] == 2

    def test_record_attempt_tracks_accessible_by(self, repo):
        repo.record_attempt("alice", "acct1", True, True, False)
        repo.record_attempt("alice", "acct2", True, True, False)
        accessible = repo.get_accessible_accounts("alice")
        assert "acct1" in accessible
        assert "acct2" in accessible

    def test_record_attempt_no_duplicate_accessible_by(self, repo):
        repo.record_attempt("alice", "acct1", True, True, False)
        repo.record_attempt("alice", "acct1", True, True, False)
        accessible = repo.get_accessible_accounts("alice")
        assert accessible.count("acct1") == 1

    def test_get_profile_summary_unknown(self, repo):
        summary = repo.get_profile_summary("nobody")
        assert summary["status"] == "unknown"
        assert summary["accessible_by"] == []

    def test_get_best_account(self, repo):
        repo.record_attempt("alice", "acct1", True, True, False)
        repo.record_attempt("alice", "acct2", True, True, False)
        best = repo.get_best_account("alice", ["acct1", "acct2"])
        assert best in ("acct1", "acct2")

    def test_get_best_account_no_success(self, repo):
        repo.record_attempt("alice", "acct1", False, False, False)
        best = repo.get_best_account("alice", ["acct1"])
        assert best is None

    def test_get_best_account_filters_available(self, repo):
        repo.record_attempt("alice", "acct1", True, True, False)
        best = repo.get_best_account("alice", ["acct2"])  # acct2 not in history
        assert best is None


class TestProfileAccessRepositoryCleanup:
    def test_cleanup_old_attempts(self, repo, db):
        # Insert an old attempt manually
        old_ts = time.time() - 40 * 86400  # 40 days ago
        db.execute(
            "INSERT INTO profile_access_attempts (target_username, accessing_account, can_access, is_public, is_followed, attempt_ts) VALUES (?,?,?,?,?,?)",
            ("alice", "acct1", 1, 1, 0, old_ts),
        )
        repo.record_attempt("alice", "acct1", True, True, False)  # recent
        removed = repo.cleanup_old_attempts(days=30)
        assert removed == 1
        rows = db.fetchall("SELECT * FROM profile_access_attempts WHERE target_username='alice'")
        assert len(rows) == 1  # only recent remains

    def test_get_statistics(self, repo):
        repo.record_attempt("alice", "acct1", True, True, False)
        repo.record_attempt("bob", "acct1", False, False, False)
        stats = repo.get_statistics()
        assert stats["total_attempts"] == 2
        assert stats["successful_attempts"] == 1
        assert stats["unique_profiles"] == 2
