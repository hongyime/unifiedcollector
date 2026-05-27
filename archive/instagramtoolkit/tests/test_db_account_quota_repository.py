"""Unit tests for AccountQuotaRepository using in-memory SQLite."""
import sys
import os
import time
import pytest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from db.manager import DatabaseManager
from db.repositories.account_quota_repository import AccountQuotaRepository


@pytest.fixture
def db():
    manager = DatabaseManager("sqlite:///:memory:")
    yield manager
    manager.close()


@pytest.fixture
def repo(db):
    return AccountQuotaRepository(db)


class TestAccountQuotaRepository:
    def test_record_profile_view(self, repo):
        repo.record_profile_view("acct1", 5)
        usage = repo.get_usage("acct1")
        assert usage["profile_views"] == 5

    def test_record_profile_view_accumulates(self, repo):
        repo.record_profile_view("acct1", 3)
        repo.record_profile_view("acct1", 7)
        usage = repo.get_usage("acct1")
        assert usage["profile_views"] == 10

    def test_record_action(self, repo):
        repo.record_action("acct1", 2)
        usage = repo.get_usage("acct1")
        assert usage["actions"] == 2

    def test_record_action_accumulates(self, repo):
        repo.record_action("acct1", 10)
        repo.record_action("acct1", 5)
        usage = repo.get_usage("acct1")
        assert usage["actions"] == 15

    def test_get_usage_default(self, repo):
        usage = repo.get_usage("new_acct")
        assert usage["profile_views"] == 0
        assert usage["actions"] == 0
        assert usage["quota_date"] == date.today().isoformat()

    def test_reset_if_new_day(self, repo, db):
        # Insert a row with yesterday's date
        yesterday = "2000-01-01"
        db.execute(
            "INSERT INTO account_quotas (account_name, quota_date, profile_views, actions, updated_at) VALUES (?,?,?,?,?)",
            ("acct1", yesterday, 100, 200, time.time()),
        )
        repo.reset_if_new_day("acct1")
        usage = repo.get_usage("acct1")
        assert usage["profile_views"] == 0
        assert usage["actions"] == 0
        assert usage["quota_date"] == date.today().isoformat()

    def test_reset_if_new_day_same_day_no_reset(self, repo):
        repo.record_profile_view("acct1", 50)
        repo.reset_if_new_day("acct1")
        usage = repo.get_usage("acct1")
        assert usage["profile_views"] == 50  # not reset

    def test_multiple_accounts_independent(self, repo):
        repo.record_profile_view("acct1", 10)
        repo.record_profile_view("acct2", 20)
        assert repo.get_usage("acct1")["profile_views"] == 10
        assert repo.get_usage("acct2")["profile_views"] == 20
