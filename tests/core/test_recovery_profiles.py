from datetime import datetime, timedelta, timezone

from src.core.recovery_profiles import (
    INSTAGRAM_RECOVERY_HOURS,
    MAX_RECOVERY_DAYS,
    _as_dict,
    observation_due,
)
from src.core.recovery_profiles import enter_recovery, get_active_recovery, record_clean_recovery_pass
from src.core.recovery_profiles import mark_recovery_observation


class _RecoveryConn:
    def __init__(self):
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return {"restricted_until": datetime.now(timezone.utc) + timedelta(hours=45), "clean_passes": 0}


class _RecoveryAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class _RecoveryPool:
    def __init__(self):
        self.conn = _RecoveryConn()

    def acquire(self):
        return _RecoveryAcquire(self.conn)


def test_recovery_profile_serializes_expiry_and_marks_active():
    now = datetime.now(timezone.utc)
    profile = _as_dict({"restricted_until": now + timedelta(hours=1), "clean_passes": 2})

    assert profile["active"] is True
    assert profile["clean"] == 2


def test_instagram_recovery_bounds_are_explicit():
    assert INSTAGRAM_RECOVERY_HOURS == 45
    assert MAX_RECOVERY_DAYS == 7


def test_recovery_observation_has_a_90_minute_floor():
    now = datetime.now(timezone.utc)
    assert observation_due({"last_observation_at": now - timedelta(minutes=89)}, now=now) is False
    assert observation_due({"last_observation_at": now - timedelta(minutes=90)}, now=now) is True


async def test_instagram_restriction_enters_a_45_hour_durable_profile():
    pool = _RecoveryPool()
    profile = await enter_recovery(pool, "instagram", account="acct", reason="challenge")

    assert profile["active"] is True
    sql, args = pool.conn.calls[0]
    assert "source_recovery_profiles" in sql
    assert args[4] == f"{45 * 60 * 60} seconds"


async def test_expired_recovery_requires_three_clean_passes_in_the_database_contract():
    pool = _RecoveryPool()
    await record_clean_recovery_pass(pool, "instagram", account="acct")

    assert "clean_passes+1 >= 3" in pool.conn.calls[0][0]
    assert "account_identity IN ($2, '')" in pool.conn.calls[0][0]
    assert "state='restricted'" in pool.conn.calls[0][0]
    assert "(account_identity = $2) DESC" in pool.conn.calls[0][0]


async def test_recovery_gate_stays_closed_after_expiry_until_clean_passes_complete():
    pool = _RecoveryPool()

    await get_active_recovery(pool, "instagram")

    assert "state='restricted'" in pool.conn.calls[0][0]
    assert "restricted_until > now()" not in pool.conn.calls[0][0]


async def test_browser_observation_falls_back_to_source_wide_recovery_profile():
    pool = _RecoveryPool()

    await mark_recovery_observation(pool, "instagram", account="logged_in_account")

    sql, args = pool.conn.calls[0]
    assert "account_identity IN ($2, '')" in sql
    assert "(account_identity = $2) DESC" in sql
    assert "interval '90 minutes'" in sql
    assert args == ("instagram", "logged_in_account")
