"""Unit tests for ProfileRepository using in-memory SQLite."""
import sys
import os
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from db.manager import DatabaseManager
from db.repositories.profile_repository import ProfileRepository


@pytest.fixture
def db():
    manager = DatabaseManager("sqlite:///:memory:")
    yield manager
    manager.close()


@pytest.fixture
def repo(db):
    return ProfileRepository(db)


def _profile(username="alice", followers=100, following=50, collected_by="acct1"):
    return {
        "username": username,
        "followers_count": followers,
        "following_count": following,
        "media_count": 10,
        "is_public": True,
        "is_verified": False,
        "collected_by": collected_by,
        "last_collected_ts": time.time(),
        "full_name": username.title(),
        "biography": "bio",
    }


class TestProfileRepositoryCRUD:
    def test_upsert_and_get(self, repo):
        repo.upsert_profile("alice", _profile("alice"))
        result = repo.get_profile("alice")
        assert result is not None
        assert result["username"] == "alice"
        assert result["followers_count"] == 100

    def test_get_nonexistent_returns_none(self, repo):
        assert repo.get_profile("nobody") is None

    def test_upsert_updates_existing(self, repo):
        repo.upsert_profile("alice", _profile("alice", followers=100))
        repo.upsert_profile("alice", _profile("alice", followers=200))
        result = repo.get_profile("alice")
        assert result["followers_count"] == 200

    def test_get_all_profiles(self, repo):
        repo.upsert_profile("alice", _profile("alice"))
        repo.upsert_profile("bob", _profile("bob", followers=50))
        all_profiles = repo.get_all_profiles()
        assert "alice" in all_profiles
        assert "bob" in all_profiles

    def test_snapshot_inserted_on_upsert(self, repo):
        repo.upsert_profile("alice", _profile("alice"))
        snaps = repo.get_snapshots("alice")
        assert len(snaps) == 1
        assert snaps[0]["followers_count"] == 100

    def test_snapshot_inserted_on_every_upsert(self, repo):
        repo.upsert_profile("alice", _profile("alice", followers=100))
        repo.upsert_profile("alice", _profile("alice", followers=200))
        repo.upsert_profile("alice", _profile("alice", followers=300))
        snaps = repo.get_snapshots("alice")
        assert len(snaps) == 3

    def test_snapshots_ordered_newest_first(self, repo):
        import time as _time
        for i in range(3):
            repo.upsert_profile("alice", _profile("alice", followers=i * 100))
            _time.sleep(0.01)  # ensure distinct timestamps
        snaps = repo.get_snapshots("alice")
        # Newest first — snapshot_ts should be descending
        for j in range(len(snaps) - 1):
            assert snaps[j]["snapshot_ts"] >= snaps[j + 1]["snapshot_ts"]

    def test_snapshots_limit(self, repo):
        for _ in range(10):
            repo.upsert_profile("alice", _profile("alice"))
        snaps = repo.get_snapshots("alice", limit=3)
        assert len(snaps) == 3

    def test_snapshots_empty_for_unknown_user(self, repo):
        assert repo.get_snapshots("nobody") == []


class TestProfileRepositoryFilters:
    def test_filter_by_follower_range(self, repo):
        repo.upsert_profile("alice", _profile("alice", followers=100))
        repo.upsert_profile("bob", _profile("bob", followers=500))
        repo.upsert_profile("carol", _profile("carol", followers=1000))

        result = repo.filter_by_follower_range(200, 800)
        assert "bob" in result
        assert "alice" not in result
        assert "carol" not in result

    def test_filter_by_follower_range_no_max(self, repo):
        repo.upsert_profile("alice", _profile("alice", followers=100))
        repo.upsert_profile("bob", _profile("bob", followers=500))
        result = repo.filter_by_follower_range(200)
        assert "bob" in result
        assert "alice" not in result

    def test_get_top_by_followers(self, repo):
        repo.upsert_profile("alice", _profile("alice", followers=100))
        repo.upsert_profile("bob", _profile("bob", followers=500))
        repo.upsert_profile("carol", _profile("carol", followers=1000))
        top = repo.get_top_by_followers(2)
        assert len(top) == 2
        assert top[0]["username"] == "carol"
        assert top[1]["username"] == "bob"

    def test_get_top_by_following(self, repo):
        repo.upsert_profile("alice", _profile("alice", following=10))
        repo.upsert_profile("bob", _profile("bob", following=999))
        top = repo.get_top_by_following(1)
        assert top[0]["username"] == "bob"
