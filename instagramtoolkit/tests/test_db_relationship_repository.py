"""Unit tests for RelationshipRepository using in-memory SQLite."""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from db.manager import DatabaseManager
from db.repositories.relationship_repository import RelationshipRepository


@pytest.fixture
def db():
    manager = DatabaseManager("sqlite:///:memory:")
    yield manager
    manager.close()


@pytest.fixture
def repo(db):
    return RelationshipRepository(db)


def _rel(source, target, rel_type="followers", collected_by="acct1", is_public=True):
    return {
        "source": source,
        "target": target,
        "type": rel_type,
        "collected_by": collected_by,
        "source_is_public": is_public,
    }


class TestRelationshipRepositoryCRUD:
    def test_upsert_relationship(self, repo):
        repo.upsert_relationship("alice", "bob", "followers", "acct1", True)
        assert repo.relationship_exists("alice", "bob", "followers")

    def test_relationship_not_exists(self, repo):
        assert not repo.relationship_exists("alice", "bob", "followers")

    def test_bulk_upsert_returns_count(self, repo):
        rels = [_rel("alice", "bob"), _rel("alice", "carol")]
        count = repo.bulk_upsert(rels)
        assert count == 2

    def test_bulk_upsert_deduplicates_within_batch(self, repo):
        rels = [_rel("alice", "bob"), _rel("alice", "bob"), _rel("alice", "carol")]
        count = repo.bulk_upsert(rels)
        assert count == 2  # deduplicated

    def test_bulk_upsert_empty(self, repo):
        assert repo.bulk_upsert([]) == 0

    def test_bulk_upsert_idempotent(self, repo):
        rels = [_rel("alice", "bob")]
        repo.bulk_upsert(rels)
        repo.bulk_upsert(rels)
        # Should still be 1 row
        rows = repo.get_relationships("alice", "followers")
        assert len(rows) == 1


class TestRelationshipRepositoryQueries:
    def test_get_followers(self, repo):
        repo.upsert_relationship("alice", "bob", "followers", "acct1", True)
        repo.upsert_relationship("carol", "bob", "followers", "acct1", True)
        followers = repo.get_followers("bob")
        assert "alice" in followers
        assert "carol" in followers

    def test_get_following(self, repo):
        repo.upsert_relationship("alice", "bob", "following", "acct1", True)
        repo.upsert_relationship("alice", "carol", "following", "acct1", True)
        following = repo.get_following("alice")
        assert "bob" in following
        assert "carol" in following

    def test_get_mutual(self, repo):
        # alice follows bob (following), bob follows alice (followers)
        repo.upsert_relationship("alice", "bob", "following", "acct1", True)
        repo.upsert_relationship("bob", "alice", "followers", "acct1", True)
        mutuals = repo.get_mutual("alice")
        assert "bob" in mutuals

    def test_get_mutual_no_mutual(self, repo):
        repo.upsert_relationship("alice", "bob", "following", "acct1", True)
        mutuals = repo.get_mutual("alice")
        assert "bob" not in mutuals

    def test_get_all_usernames(self, repo):
        repo.upsert_relationship("alice", "bob", "followers", "acct1", True)
        repo.upsert_relationship("carol", "dave", "following", "acct1", True)
        usernames = repo.get_all_usernames()
        assert "alice" in usernames
        assert "bob" in usernames
        assert "carol" in usernames
        assert "dave" in usernames

    def test_get_relationships_filtered(self, repo):
        repo.upsert_relationship("alice", "bob", "followers", "acct1", True)
        repo.upsert_relationship("alice", "carol", "following", "acct1", True)
        rows = repo.get_relationships("alice", "followers")
        assert len(rows) == 1
        assert rows[0]["target"] == "bob"
