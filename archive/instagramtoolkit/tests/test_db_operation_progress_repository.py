"""Unit tests for OperationProgressRepository using in-memory SQLite."""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from db.manager import DatabaseManager
from db.repositories.operation_progress_repository import OperationProgressRepository


@pytest.fixture
def db():
    manager = DatabaseManager("sqlite:///:memory:")
    yield manager
    manager.close()


@pytest.fixture
def repo(db):
    return OperationProgressRepository(db)


OP_ID = "test_op_001"


class TestOperationProgressRepositoryCRUD:
    def test_upsert_and_get_status(self, repo):
        repo.upsert_progress(OP_ID, "alice", "pending")
        assert repo.get_status(OP_ID, "alice") == "pending"

    def test_get_status_nonexistent(self, repo):
        assert repo.get_status(OP_ID, "nobody") is None

    def test_upsert_updates_status(self, repo):
        repo.upsert_progress(OP_ID, "alice", "pending")
        repo.upsert_progress(OP_ID, "alice", "completed")
        assert repo.get_status(OP_ID, "alice") == "completed"

    def test_get_completed(self, repo):
        repo.upsert_progress(OP_ID, "alice", "completed")
        repo.upsert_progress(OP_ID, "bob", "failed")
        completed = repo.get_completed(OP_ID)
        assert "alice" in completed
        assert "bob" not in completed

    def test_get_failed(self, repo):
        repo.upsert_progress(OP_ID, "alice", "failed")
        failed = repo.get_failed(OP_ID)
        assert "alice" in failed

    def test_get_pending(self, repo):
        repo.upsert_progress(OP_ID, "alice", "pending")
        pending = repo.get_pending(OP_ID)
        assert "alice" in pending

    def test_get_remaining(self, repo):
        repo.upsert_progress(OP_ID, "alice", "completed")
        repo.upsert_progress(OP_ID, "bob", "failed")
        remaining = repo.get_remaining(OP_ID, ["alice", "bob", "carol"])
        assert "carol" in remaining
        assert "alice" not in remaining
        assert "bob" not in remaining

    def test_get_remaining_preserves_order(self, repo):
        repo.upsert_progress(OP_ID, "alice", "completed")
        all_users = ["carol", "alice", "bob"]
        remaining = repo.get_remaining(OP_ID, all_users)
        assert remaining == ["carol", "bob"]

    def test_get_statistics(self, repo):
        repo.upsert_progress(OP_ID, "alice", "completed")
        repo.upsert_progress(OP_ID, "bob", "failed")
        repo.upsert_progress(OP_ID, "carol", "pending")
        stats = repo.get_statistics(OP_ID)
        assert stats["completed"] == 1
        assert stats["failed"] == 1
        assert stats["pending"] == 1


class TestBatchState:
    def test_upsert_and_get_batch_state(self, repo):
        state = {"current_user_index": 5, "total_users": 100, "operation_type": "spider"}
        repo.upsert_batch_state(OP_ID, state)
        result = repo.get_batch_state(OP_ID)
        assert result["current_user_index"] == 5
        assert result["total_users"] == 100

    def test_get_batch_state_empty(self, repo):
        assert repo.get_batch_state("nonexistent") == {}

    def test_upsert_batch_state_updates(self, repo):
        repo.upsert_batch_state(OP_ID, {"current_user_index": 1, "operation_type": "spider"})
        repo.upsert_batch_state(OP_ID, {"current_user_index": 10, "operation_type": "spider"})
        result = repo.get_batch_state(OP_ID)
        assert result["current_user_index"] == 10


class TestArchiveOperation:
    def test_archive_deletes_only_target_operation(self, repo):
        repo.upsert_progress(OP_ID, "alice", "completed")
        repo.upsert_progress("other_op", "bob", "completed")
        repo.archive_operation(OP_ID)
        assert repo.get_status(OP_ID, "alice") is None
        assert repo.get_status("other_op", "bob") == "completed"

    def test_archive_deletes_batch_state(self, repo):
        repo.upsert_batch_state(OP_ID, {"operation_type": "spider"})
        repo.archive_operation(OP_ID)
        assert repo.get_batch_state(OP_ID) == {}
