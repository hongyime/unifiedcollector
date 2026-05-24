"""Unit tests for AccountCooldownRepository using in-memory SQLite."""
import sys
import os
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from db.manager import DatabaseManager
from db.repositories.account_cooldown_repository import AccountCooldownRepository


@pytest.fixture
def db():
    manager = DatabaseManager("sqlite:///:memory:")
    yield manager
    manager.close()


@pytest.fixture
def repo(db):
    return AccountCooldownRepository(db)


class TestAccountCooldownRepository:
    def test_put_on_cooldown_and_is_on_cooldown(self, repo):
        until = time.time() + 3600  # 1 hour from now
        repo.put_on_cooldown("acct1", until, "rate-limit")
        assert repo.is_on_cooldown("acct1") is True

    def test_not_on_cooldown_when_absent(self, repo):
        assert repo.is_on_cooldown("acct1") is False

    def test_expired_cooldown_returns_false_and_deletes(self, repo, db):
        past = time.time() - 10  # already expired
        repo.put_on_cooldown("acct1", past, "rate-limit")
        assert repo.is_on_cooldown("acct1") is False
        # Row should be deleted
        row = db.fetchone("SELECT * FROM account_cooldowns WHERE account_name='acct1'")
        assert row is None

    def test_get_remaining_active(self, repo):
        until = time.time() + 3600
        repo.put_on_cooldown("acct1", until, "rate-limit")
        remaining = repo.get_remaining("acct1")
        assert remaining > 0
        assert remaining <= 3600

    def test_get_remaining_absent(self, repo):
        assert repo.get_remaining("acct1") == 0.0

    def test_clear_cooldown(self, repo, db):
        until = time.time() + 3600
        repo.put_on_cooldown("acct1", until, "rate-limit")
        repo.clear_cooldown("acct1")
        assert repo.is_on_cooldown("acct1") is False
        row = db.fetchone("SELECT * FROM account_cooldowns WHERE account_name='acct1'")
        assert row is None

    def test_clear_cooldown_nonexistent(self, repo):
        # Should not raise
        repo.clear_cooldown("nobody")

    def test_get_available_filters_cooldown(self, repo):
        until = time.time() + 3600
        repo.put_on_cooldown("acct1", until, "rate-limit")
        available = repo.get_available(["acct1", "acct2"])
        assert "acct2" in available
        assert "acct1" not in available

    def test_get_available_all_available(self, repo):
        available = repo.get_available(["acct1", "acct2"])
        assert available == ["acct1", "acct2"]

    def test_put_on_cooldown_replaces_existing(self, repo):
        until1 = time.time() + 100
        until2 = time.time() + 7200
        repo.put_on_cooldown("acct1", until1, "rate-limit")
        repo.put_on_cooldown("acct1", until2, "manual")
        remaining = repo.get_remaining("acct1")
        assert remaining > 100  # updated to longer cooldown
