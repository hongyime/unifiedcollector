import sys, os, time, json, pytest
from datetime import date
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
from hypothesis import given, settings, assume
from hypothesis import strategies as st
from db.manager import DatabaseManager
from db.repositories.profile_repository import ProfileRepository
from db.repositories.relationship_repository import RelationshipRepository
from db.repositories.profile_access_repository import ProfileAccessRepository
from db.repositories.operation_progress_repository import OperationProgressRepository
from db.repositories.account_cooldown_repository import AccountCooldownRepository
from db.repositories.account_quota_repository import AccountQuotaRepository
from db.repositories.username_repository import UsernameRepository

username_st = st.text(alphabet=st.characters(whitelist_categories=('Lu','Ll','Nd'), whitelist_characters='._'), min_size=1, max_size=30).filter(lambda s: s.strip() and not s.startswith('.') and not s.endswith('.'))
account_st = st.text(alphabet='abcdefghijklmnopqrstuvwxyz0123456789', min_size=1, max_size=20)
follower_count_st = st.integers(min_value=0, max_value=10_000_000)
rel_type_st = st.sampled_from(['followers', 'following'])
status_st = st.sampled_from(['pending', 'completed', 'failed'])

def fresh_db(): return DatabaseManager('sqlite:///:memory:')

@settings(max_examples=100)
@given(st.integers(min_value=1, max_value=5))
def test_property_1_schema_idempotent(n_calls):
    db = fresh_db()
    for _ in range(n_calls): db.create_schema()
    tables = db.fetchall('SELECT name FROM sqlite_master WHERE type=\'table\'')
    assert 'profiles' in {r['name'] for r in tables}
    db.close()

@settings(max_examples=100)
@given(username_st, follower_count_st, follower_count_st, account_st)
def test_property_2_profile_upsert_roundtrip(username, followers, following, account):
    db = fresh_db()
    repo = ProfileRepository(db)
    repo.upsert_profile(username, {'followers_count': followers, 'following_count': following, 'collected_by': account, 'last_collected_ts': time.time()})
    result = repo.get_profile(username)
    assert result is not None and result['username'] == username and result['followers_count'] == followers
    db.close()

@settings(max_examples=100)
@given(username_st, st.integers(min_value=1, max_value=20), account_st)
def test_property_3_upsert_produces_snapshot(username, n_calls, account):
    db = fresh_db()
    repo = ProfileRepository(db)
    for i in range(n_calls):
        repo.upsert_profile(username, {'followers_count': i, 'following_count': 0, 'collected_by': account, 'last_collected_ts': time.time()})
    assert len(repo.get_snapshots(username, limit=n_calls+1)) == n_calls
    db.close()

@settings(max_examples=100)
@given(username_st, st.integers(min_value=1, max_value=10), st.integers(min_value=1, max_value=5), account_st)
def test_property_4_snapshots_ordering_and_limit(username, n_inserts, limit, account):
    db = fresh_db()
    repo = ProfileRepository(db)
    for i in range(n_inserts):
        repo.upsert_profile(username, {'followers_count': i*10, 'following_count': 0, 'collected_by': account, 'last_collected_ts': time.time()})
    snaps = repo.get_snapshots(username, limit=limit)
    assert len(snaps) <= limit and len(snaps) <= n_inserts
    for i in range(len(snaps)-1): assert snaps[i]['snapshot_ts'] >= snaps[i+1]['snapshot_ts']
    db.close()

@settings(max_examples=100)
@given(st.lists(st.tuples(username_st, follower_count_st), min_size=1, max_size=10, unique_by=lambda x: x[0]), follower_count_st, follower_count_st, account_st)
def test_property_5_filter_by_follower_range(profiles, min_f, max_f, account):
    if min_f > max_f: min_f, max_f = max_f, min_f
    db = fresh_db()
    repo = ProfileRepository(db)
    for u, f in profiles:
        repo.upsert_profile(u, {'followers_count': f, 'following_count': 0, 'collected_by': account, 'last_collected_ts': time.time()})
    for u in repo.filter_by_follower_range(min_f, max_f):
        p = repo.get_profile(u)
        assert min_f <= p['followers_count'] <= max_f
    db.close()

@settings(max_examples=100)
@given(st.lists(st.tuples(username_st, username_st, rel_type_st), min_size=0, max_size=20), account_st)
def test_property_6_bulk_upsert_deduplication(rels_raw, account):
    db = fresh_db()
    repo = RelationshipRepository(db)
    rels = [{'source': s, 'target': t, 'type': rt, 'collected_by': account, 'source_is_public': True} for s, t, rt in rels_raw]
    count = repo.bulk_upsert(rels)
    assert count == len({(r['source'], r['target'], r['type']) for r in rels})
    db.close()

@settings(max_examples=100)
@given(username_st, st.lists(username_st, min_size=0, max_size=10), st.lists(username_st, min_size=0, max_size=10), account_st)
def test_property_7_get_mutual_is_intersection(username, followers, following, account):
    db = fresh_db()
    repo = RelationshipRepository(db)
    for f in followers:
        if f != username: repo.upsert_relationship(f, username, 'followers', account, True)
    for f in following:
        if f != username: repo.upsert_relationship(username, f, 'following', account, True)
    assert set(repo.get_mutual(username)) == (set(followers) & set(following)) - {username}
    db.close()

@settings(max_examples=100)
@given(username_st, username_st, rel_type_st, account_st)
def test_property_8_relationship_exists_roundtrip(source, target, rel_type, account):
    db = fresh_db()
    repo = RelationshipRepository(db)
    assert not repo.relationship_exists(source, target, rel_type)
    repo.upsert_relationship(source, target, rel_type, account, True)
    assert repo.relationship_exists(source, target, rel_type)
    db.close()

@settings(max_examples=100)
@given(username_st, st.integers(min_value=1, max_value=20), account_st)
def test_property_9_record_attempt_monotonic(username, n_attempts, account):
    db = fresh_db()
    repo = ProfileAccessRepository(db)
    for i in range(n_attempts): repo.record_attempt(username, account, i%2==0, True, False)
    assert repo.get_profile_summary(username)['total_attempts'] == n_attempts
    db.close()

@settings(max_examples=100)
@given(username_st, st.integers(min_value=1, max_value=10), st.integers(min_value=1, max_value=10), account_st)
def test_property_10_cleanup_removes_only_expired(username, old_count, recent_count, account):
    db = fresh_db()
    repo = ProfileAccessRepository(db)
    old_ts = time.time() - 40*86400
    for _ in range(old_count):
        db.execute('INSERT INTO profile_access_attempts (target_username, accessing_account, can_access, is_public, is_followed, attempt_ts) VALUES (?,?,?,?,?,?)', (username, account, 0, 1, 0, old_ts))
    for _ in range(recent_count): repo.record_attempt(username, account, False, True, False)
    assert repo.cleanup_old_attempts(days=30) == old_count
    assert len(db.fetchall('SELECT * FROM profile_access_attempts WHERE target_username=?', (username,))) == recent_count
    db.close()

@settings(max_examples=100)
@given(st.text(min_size=1, max_size=20), st.lists(username_st, min_size=0, max_size=10, unique=True), st.lists(username_st, min_size=0, max_size=10, unique=True), status_st)
def test_property_11_get_remaining_set_difference(op_id, done_users, all_extra, done_status):
    assume(done_status in ('completed', 'failed'))
    db = fresh_db()
    repo = OperationProgressRepository(db)
    for u in done_users: repo.upsert_progress(op_id, u, done_status)
    all_users = list(dict.fromkeys(done_users + all_extra))
    remaining = repo.get_remaining(op_id, all_users)
    done_set = set(done_users)
    for u in remaining: assert u not in done_set
    for u in all_users:
        if u not in done_set: assert u in remaining
    db.close()

@settings(max_examples=100)
@given(st.text(min_size=1, max_size=20), st.text(min_size=1, max_size=20), username_st, username_st)
def test_property_12_archive_isolates_deletion(op1, op2, user1, user2):
    assume(op1 != op2)
    db = fresh_db()
    repo = OperationProgressRepository(db)
    repo.upsert_progress(op1, user1, 'completed')
    repo.upsert_progress(op2, user2, 'completed')
    repo.archive_operation(op1)
    assert repo.get_status(op1, user1) is None
    assert repo.get_status(op2, user2) == 'completed'
    db.close()

@settings(max_examples=100)
@given(st.text(min_size=1, max_size=20), st.fixed_dictionaries({'current_user_index': st.integers(min_value=0, max_value=1000), 'total_users': st.integers(min_value=0, max_value=1000), 'operation_type': st.sampled_from(['spider', 'download', 'general'])}))
def test_property_13_batch_state_roundtrip(op_id, state):
    db = fresh_db()
    repo = OperationProgressRepository(db)
    repo.upsert_batch_state(op_id, state)
    result = repo.get_batch_state(op_id)
    for k, v in state.items(): assert result[k] == v
    db.close()

@settings(max_examples=100)
@given(account_st, st.floats(min_value=1.0, max_value=7200.0))
def test_property_14_cooldown_reflects_until_ts(account, duration_seconds):
    db = fresh_db()
    repo = AccountCooldownRepository(db)
    repo.put_on_cooldown(account, time.time() + duration_seconds, 'test')
    assert repo.is_on_cooldown(account) is True
    db.close()

@settings(max_examples=100)
@given(st.lists(account_st, min_size=1, max_size=10, unique=True), st.lists(account_st, min_size=0, max_size=5, unique=True))
def test_property_15_get_available_filters_cooldown(all_accounts, cooldown_accounts):
    db = fresh_db()
    repo = AccountCooldownRepository(db)
    on_cooldown = set()
    for acct in cooldown_accounts:
        if acct in all_accounts:
            repo.put_on_cooldown(acct, time.time() + 3600, 'test')
            on_cooldown.add(acct)
    for acct in repo.get_available(all_accounts): assert acct not in on_cooldown
    db.close()

@settings(max_examples=100)
@given(account_st, st.lists(st.integers(min_value=1, max_value=100), min_size=1, max_size=20))
def test_property_16_quota_accumulation(account, view_counts):
    db = fresh_db()
    repo = AccountQuotaRepository(db)
    for c in view_counts: repo.record_profile_view(account, c)
    assert repo.get_usage(account)['profile_views'] == sum(view_counts)
    db.close()

@settings(max_examples=100)
@given(username_st, account_st)
def test_property_17_add_username_idempotent(username, account):
    db = fresh_db()
    repo = UsernameRepository(db)
    assert repo.add_username(username, account) is True
    assert repo.add_username(username, account) is False
    assert len(db.fetchall('SELECT * FROM usernames WHERE username=?', (username,))) == 1
    db.close()

@settings(max_examples=100)
@given(username_st, account_st)
def test_property_18_exists_roundtrip(username, account):
    db = fresh_db()
    repo = UsernameRepository(db)
    assert not repo.exists(username)
    repo.add_username(username, account)
    assert repo.exists(username)
    db.close()

@settings(max_examples=100)
@given(username_st, account_st, st.booleans())
def test_property_19_following_status_roundtrip(username, account, following):
    db = fresh_db()
    repo = UsernameRepository(db)
    repo.add_username(username, account)
    repo.update_following_status(username, account, following)
    row = db.fetchone('SELECT is_following FROM username_following_status WHERE username=? AND account_name=?', (username, account))
    assert row is not None and bool(row['is_following']) == following
    db.close()

@settings(max_examples=100)
@given(st.lists(st.tuples(username_st, follower_count_st, follower_count_st), min_size=0, max_size=10, unique_by=lambda x: x[0]))
def test_property_20_migration_inserts_all_records(profiles):
    import tempfile
    from db.migrate_json import migrate_json_to_db
    db = fresh_db()
    with tempfile.TemporaryDirectory() as tmp:
        data = {u: {'followers_count': f, 'following_count': fo, 'collected_by': 'acct1', 'last_collected_ts': time.time()} for u, f, fo in profiles}
        path = os.path.join(tmp, 'user_profiles.json')
        with open(path, 'w') as fp: json.dump(data, fp)
        report = migrate_json_to_db(tmp, db)
        assert report['migrated'].get('profiles', 0) == len(profiles)
    db.close()

@settings(max_examples=100)
@given(st.lists(st.tuples(username_st, follower_count_st), min_size=1, max_size=5, unique_by=lambda x: x[0]))
def test_property_21_migration_idempotent(profiles):
    import tempfile
    from db.migrate_json import migrate_json_to_db
    db = fresh_db()
    with tempfile.TemporaryDirectory() as tmp:
        data = {u: {'followers_count': f, 'following_count': 0, 'collected_by': 'acct1', 'last_collected_ts': time.time()} for u, f in profiles}
        path = os.path.join(tmp, 'user_profiles.json')
        with open(path, 'w') as fp: json.dump(data, fp)
        migrate_json_to_db(tmp, db)
        os.rename(path + '.bak', path)
        migrate_json_to_db(tmp, db)
        assert len(db.fetchall('SELECT * FROM profiles')) == len(profiles)
    db.close()
