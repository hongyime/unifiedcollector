"""Unit tests for migrate_json_to_db using tmp_path fixtures."""
import sys
import os
import json
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from db.manager import DatabaseManager
from db.migrate_json import migrate_json_to_db


@pytest.fixture
def db():
    manager = DatabaseManager("sqlite:///:memory:")
    yield manager
    manager.close()


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class TestMigrateProfiles:
    def test_migrates_profiles(self, db, data_dir):
        profiles = {
            "alice": {"followers_count": 100, "following_count": 50, "collected_by": "acct1", "last_collected_ts": time.time()},
            "bob": {"followers_count": 200, "following_count": 80, "collected_by": "acct1", "last_collected_ts": time.time()},
        }
        write_json(data_dir / "user_profiles.json", profiles)
        report = migrate_json_to_db(str(data_dir), db)
        assert report["migrated"]["profiles"] == 2

    def test_renames_to_bak(self, db, data_dir):
        write_json(data_dir / "user_profiles.json", {"alice": {"followers_count": 10, "following_count": 5, "collected_by": "acct1", "last_collected_ts": time.time()}})
        migrate_json_to_db(str(data_dir), db)
        assert not (data_dir / "user_profiles.json").exists()
        assert (data_dir / "user_profiles.json.bak").exists()

    def test_skips_missing_file(self, db, data_dir):
        report = migrate_json_to_db(str(data_dir), db)
        assert "user_profiles.json" in report["skipped"]

    def test_idempotent(self, db, data_dir):
        profiles = {"alice": {"followers_count": 100, "following_count": 50, "collected_by": "acct1", "last_collected_ts": time.time()}}
        write_json(data_dir / "user_profiles.json", profiles)
        migrate_json_to_db(str(data_dir), db)
        # Restore bak and run again
        os.rename(data_dir / "user_profiles.json.bak", data_dir / "user_profiles.json")
        report2 = migrate_json_to_db(str(data_dir), db)
        # Should still be 1 profile (upsert)
        rows = db.fetchall("SELECT * FROM profiles")
        assert len(rows) == 1


class TestMigrateRelationships:
    def test_migrates_relationships(self, db, data_dir):
        rels = [
            {"source": "alice", "target": "bob", "type": "followers", "collected_by_account": "acct1", "source_is_public": True},
            {"source": "alice", "target": "carol", "type": "following", "collected_by_account": "acct1", "source_is_public": True},
        ]
        write_json(data_dir / "relationships.json", rels)
        report = migrate_json_to_db(str(data_dir), db)
        assert report["migrated"]["relationships"] == 2

    def test_renames_to_bak(self, db, data_dir):
        write_json(data_dir / "relationships.json", [])
        migrate_json_to_db(str(data_dir), db)
        assert (data_dir / "relationships.json.bak").exists()


class TestMigrateUsernames:
    def test_migrates_usernames_txt(self, db, data_dir):
        (data_dir / "usernames.txt").write_text("alice\nbob\ncarol\n")
        report = migrate_json_to_db(str(data_dir), db)
        assert report["migrated"]["usernames_txt"] == 3

    def test_renames_usernames_txt_to_bak(self, db, data_dir):
        (data_dir / "usernames.txt").write_text("alice\n")
        migrate_json_to_db(str(data_dir), db)
        assert (data_dir / "usernames.txt.bak").exists()


class TestMigrateCooldowns:
    def test_migrates_cooldowns(self, db, data_dir):
        cooldowns = {
            "acct1": {"until": time.time() + 3600, "reason": "rate-limit"},
        }
        write_json(data_dir / "account_cooldowns.json", cooldowns)
        report = migrate_json_to_db(str(data_dir), db)
        assert report["migrated"]["account_cooldowns"] == 1

    def test_renames_to_bak(self, db, data_dir):
        write_json(data_dir / "account_cooldowns.json", {})
        migrate_json_to_db(str(data_dir), db)
        assert (data_dir / "account_cooldowns.json.bak").exists()


class TestMigrateQuotas:
    def test_migrates_quotas(self, db, data_dir):
        quotas = {
            "acct1": {"date": "2024-01-01", "profile_views": 50, "actions": 100},
        }
        write_json(data_dir / "account_quotas.json", quotas)
        report = migrate_json_to_db(str(data_dir), db)
        assert report["migrated"]["account_quotas"] == 1


class TestSafetyGuarantees:
    def test_env_file_untouched(self, db, data_dir, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SECRET=value\n")
        migrate_json_to_db(str(data_dir), db)
        assert env_path.exists()
        assert env_path.read_text() == "SECRET=value\n"

    def test_data_dir_itself_not_deleted(self, db, data_dir):
        migrate_json_to_db(str(data_dir), db)
        assert data_dir.exists()

    def test_error_isolation(self, db, data_dir):
        # One bad record should not abort the rest
        profiles = {
            "alice": {"followers_count": 100, "following_count": 50, "collected_by": "acct1", "last_collected_ts": time.time()},
            "": {"followers_count": "bad"},  # empty username — will cause issues
        }
        write_json(data_dir / "user_profiles.json", profiles)
        report = migrate_json_to_db(str(data_dir), db)
        # alice should still be migrated
        assert report["migrated"]["profiles"] >= 1
