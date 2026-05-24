import sys, os, time, json, pytest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))

from db.manager import DatabaseManager
from db.repositories.profile_repository import ProfileRepository
from db.repositories.relationship_repository import RelationshipRepository
from db.repositories.profile_access_repository import ProfileAccessRepository
from db.repositories.operation_progress_repository import OperationProgressRepository
from db.repositories.account_cooldown_repository import AccountCooldownRepository
from db.repositories.account_quota_repository import AccountQuotaRepository
from db.repositories.username_repository import UsernameRepository
from db.migrate_json import migrate_json_to_db


@pytest.fixture
def db_file(tmp_path):
    db_path = str(tmp_path / 'test.db')
    db = DatabaseManager('sqlite:///' + db_path)
    yield db
    db.close()


@pytest.fixture
def db():
    manager = DatabaseManager('sqlite:///:memory:')
    yield manager
    manager.close()


class TestProfileManagerIntegration:
    def test_update_profile_writes_to_profiles_and_snapshots(self, db):
        repo = ProfileRepository(db)
        data = {'followers_count': 500, 'following_count': 100, 'collected_by': 'acct1', 'last_collected_ts': time.time(), 'is_public': True, 'is_verified': False, 'media_count': 20}
        repo.upsert_profile('alice', data)
        profile = repo.get_profile('alice')
        assert profile is not None
        assert profile['followers_count'] == 500
        snaps = repo.get_snapshots('alice')
        assert len(snaps) == 1
        assert snaps[0]['followers_count'] == 500

    def test_update_profile_multiple_times_creates_multiple_snapshots(self, db):
        repo = ProfileRepository(db)
        for followers in [100, 200, 300]:
            repo.upsert_profile('alice', {'followers_count': followers, 'following_count': 0, 'collected_by': 'acct1', 'last_collected_ts': time.time()})
        assert len(repo.get_snapshots('alice')) == 3
        assert repo.get_profile('alice')['followers_count'] == 300


class TestRelationshipCollectorIntegration:
    def test_bulk_upsert_writes_to_relationships_and_usernames(self, db):
        rel_repo = RelationshipRepository(db)
        usr_repo = UsernameRepository(db)
        rels = [
            {'source': 'alice', 'target': 'bob', 'type': 'following', 'collected_by': 'acct1', 'source_is_public': True},
            {'source': 'alice', 'target': 'carol', 'type': 'following', 'collected_by': 'acct1', 'source_is_public': True},
        ]
        rel_repo.bulk_upsert(rels)
        usr_repo.add_username('alice', 'acct1')
        usr_repo.add_username('bob', 'acct1')
        usr_repo.add_username('carol', 'acct1')
        assert rel_repo.relationship_exists('alice', 'bob', 'following')
        assert usr_repo.exists('alice')
        assert usr_repo.exists('bob')


class TestProgressManagerIntegration:
    def test_mark_completed_removes_from_remaining(self, db):
        repo = OperationProgressRepository(db)
        op_id = 'test_op'
        repo.upsert_progress(op_id, 'alice', 'pending')
        repo.upsert_progress(op_id, 'bob', 'pending')
        repo.upsert_progress(op_id, 'alice', 'completed')
        remaining = repo.get_remaining(op_id, ['alice', 'bob', 'carol'])
        assert 'alice' not in remaining
        assert 'bob' in remaining
        assert 'carol' in remaining

    def test_batch_state_roundtrip(self, db):
        repo = OperationProgressRepository(db)
        state = {'current_user_index': 42, 'total_users': 100, 'operation_type': 'spider'}
        repo.upsert_batch_state('op1', state)
        result = repo.get_batch_state('op1')
        assert result['current_user_index'] == 42


class TestCooldownIntegration:
    def test_cooldown_lifecycle(self, db):
        repo = AccountCooldownRepository(db)
        repo.put_on_cooldown('acct1', time.time() + 3600, 'rate-limit')
        assert repo.is_on_cooldown('acct1')
        repo.clear_cooldown('acct1')
        assert not repo.is_on_cooldown('acct1')

    def test_quota_accumulation(self, db):
        repo = AccountQuotaRepository(db)
        repo.record_profile_view('acct1', 10)
        repo.record_action('acct1', 5)
        usage = repo.get_usage('acct1')
        assert usage['profile_views'] == 10
        assert usage['actions'] == 5


class TestMigrationIntegration:
    def test_migration_produces_correct_db_state(self, db, tmp_path):
        profiles = {
            'alice': {'followers_count': 100, 'following_count': 50, 'collected_by': 'acct1', 'last_collected_ts': time.time()},
            'bob': {'followers_count': 200, 'following_count': 80, 'collected_by': 'acct1', 'last_collected_ts': time.time()},
        }
        rels = [
            {'source': 'alice', 'target': 'bob', 'type': 'following', 'collected_by_account': 'acct1', 'source_is_public': True},
        ]
        (tmp_path / 'user_profiles.json').write_text(json.dumps(profiles))
        (tmp_path / 'relationships.json').write_text(json.dumps(rels))
        (tmp_path / 'usernames.txt').write_text('alice\nbob\ncarol\n')
        report = migrate_json_to_db(str(tmp_path), db)
        assert report['migrated']['profiles'] == 2
        assert report['migrated']['relationships'] == 1
        assert report['migrated']['usernames_txt'] == 3
        repo = ProfileRepository(db)
        assert repo.get_profile('alice')['followers_count'] == 100
        assert repo.get_profile('bob')['followers_count'] == 200

    def test_migration_bak_files_created(self, db, tmp_path):
        (tmp_path / 'user_profiles.json').write_text(json.dumps({'alice': {'followers_count': 10, 'following_count': 5, 'collected_by': 'acct1', 'last_collected_ts': time.time()}}))
        migrate_json_to_db(str(tmp_path), db)
        assert (tmp_path / 'user_profiles.json.bak').exists()
        assert not (tmp_path / 'user_profiles.json').exists()

    def test_migration_idempotent(self, db, tmp_path):
        profiles = {'alice': {'followers_count': 100, 'following_count': 50, 'collected_by': 'acct1', 'last_collected_ts': time.time()}}
        (tmp_path / 'user_profiles.json').write_text(json.dumps(profiles))
        migrate_json_to_db(str(tmp_path), db)
        os.rename(str(tmp_path / 'user_profiles.json.bak'), str(tmp_path / 'user_profiles.json'))
        migrate_json_to_db(str(tmp_path), db)
        rows = db.fetchall('SELECT * FROM profiles')
        assert len(rows) == 1

    def test_migration_skips_missing_files(self, db, tmp_path):
        report = migrate_json_to_db(str(tmp_path), db)
        assert 'user_profiles.json' in report['skipped']
        assert 'relationships.json' in report['skipped']
