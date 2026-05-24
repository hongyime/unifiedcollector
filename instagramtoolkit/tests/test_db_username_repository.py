"""Unit tests for UsernameRepository using in-memory SQLite."""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from db.manager import DatabaseManager
from db.repositories.username_repository import UsernameRepository


@pytest.fixture
def db():
    manager = DatabaseManager("sqlite:///:memory:")
    yield manager
    manager.close()


@pytest.fixture
def repo(db):
    return UsernameRepository(db)


class TestUsernameRepositoryCRUD:
    def test_add_username_returns_true(self, repo):
        assert repo.add_username("alice", "acct1") is True

    def test_add_username_duplicate_returns_false(self, repo):
        repo.add_username("alice", "acct1")
        assert repo.add_username("alice", "acct1") is False

    def test_exists_after_add(self, repo):
        repo.add_username("alice", "acct1")
        assert repo.exists("alice") is True

    def test_exists_not_added(self, repo):
        assert repo.exists("nobody") is False

    def test_get_all(self, repo):
        repo.add_username("alice", "acct1")
        repo.add_username("bob", "acct2")
        all_rows = repo.get_all()
        usernames = [r["username"] for r in all_rows]
        assert "alice" in usernames
        assert "bob" in usernames

    def test_get_by_source(self, repo):
        repo.add_username("alice", "acct1")
        repo.add_username("bob", "acct2")
        rows = repo.get_by_source("acct1")
        assert len(rows) == 1
        assert rows[0]["username"] == "alice"

    def test_update_metadata(self, repo):
        repo.add_username("alice", "acct1", {"tag": "original"})
        repo.update_metadata("alice", {"tag": "updated", "extra": "value"})
        import json
        row = repo._db.fetchone("SELECT metadata_json FROM usernames WHERE username='alice'")
        meta = json.loads(row["metadata_json"])
        assert meta["tag"] == "updated"
        assert meta["extra"] == "value"

    def test_update_metadata_nonexistent(self, repo):
        assert repo.update_metadata("nobody", {"x": 1}) is False

    def test_update_last_accessed(self, repo):
        repo.add_username("alice", "acct1")
        assert repo.update_last_accessed("alice") is True
        row = repo._db.fetchone("SELECT last_accessed_ts FROM usernames WHERE username='alice'")
        assert row["last_accessed_ts"] is not None

    def test_update_last_accessed_nonexistent(self, repo):
        assert repo.update_last_accessed("nobody") is False


class TestFollowingStatus:
    def test_update_following_status(self, repo, db):
        repo.add_username("alice", "acct1")
        repo.update_following_status("alice", "acct1", True)
        row = db.fetchone(
            "SELECT is_following FROM username_following_status WHERE username='alice' AND account_name='acct1'"
        )
        assert row is not None
        assert row["is_following"] == 1

    def test_update_following_status_nonexistent_username(self, repo):
        assert repo.update_following_status("nobody", "acct1", True) is False

    def test_update_following_status_upsert(self, repo):
        repo.add_username("alice", "acct1")
        repo.update_following_status("alice", "acct1", True)
        repo.update_following_status("alice", "acct1", False)
        row = repo._db.fetchone(
            "SELECT is_following FROM username_following_status WHERE username='alice' AND account_name='acct1'"
        )
        assert row["is_following"] == 0


class TestRemove:
    def test_remove_username(self, repo):
        repo.add_username("alice", "acct1")
        assert repo.remove("alice") is True
        assert repo.exists("alice") is False

    def test_remove_nonexistent(self, repo):
        assert repo.remove("nobody") is False

    def test_remove_cascades_following_status(self, repo, db):
        repo.add_username("alice", "acct1")
        repo.update_following_status("alice", "acct1", True)
        repo.remove("alice")
        row = db.fetchone(
            "SELECT * FROM username_following_status WHERE username='alice'"
        )
        assert row is None
