"""Unit tests for src.core.tls_fingerprint."""
import pytest

from src.core.tls_fingerprint import (
    TLSFingerprintRotator,
    DEFAULT_IMPERSONATES,
)


class FakeClock:
    def __init__(self, t0: float = 1_000_000.0):
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float):
        self.t += secs


# ---- construction & deterministic init ---------------------------------

def test_requires_account_id():
    with pytest.raises(ValueError):
        TLSFingerprintRotator(account_id="", available_impersonates=["chrome120"])


def test_rejects_empty_impersonate_list():
    with pytest.raises(ValueError):
        TLSFingerprintRotator(account_id="acct1", available_impersonates=[])


def test_initial_selection_is_deterministic():
    a = TLSFingerprintRotator("acct1", ["chrome120", "safari17_2", "edge101"])
    b = TLSFingerprintRotator("acct1", ["chrome120", "safari17_2", "edge101"])
    assert a.get_current() == b.get_current()


def test_different_accounts_can_get_different_pins():
    pool = ["chrome120", "safari17_2", "edge101", "chrome119"]
    pins = {
        TLSFingerprintRotator(f"acct{i}", pool).get_current()
        for i in range(20)
    }
    # With 20 accounts and 4 options, at least 2 distinct pins is overwhelmingly likely
    assert len(pins) >= 2


def test_get_curl_cffi_kwargs_shape():
    r = TLSFingerprintRotator("acct1", ["chrome120"])
    kw = r.get_curl_cffi_kwargs()
    assert kw == {"impersonate": "chrome120"}


def test_default_impersonates_used_when_none_passed():
    r = TLSFingerprintRotator("acct1")
    assert r.get_current() in DEFAULT_IMPERSONATES


# ---- rotation cooldown -------------------------------------------------

def test_rotate_advances_when_cooldown_clear():
    clk = FakeClock()
    r = TLSFingerprintRotator(
        "acct1", ["chrome120", "safari17_2", "edge101"],
        cooldown_secs=600, clock=clk,
    )
    before = r.get_current()
    after = r.rotate_on_failure(reason="429")
    assert after != before
    assert r._rotation_count == 1
    assert r._last_failure_reason == "429"


def test_rotate_under_cooldown_is_noop():
    clk = FakeClock()
    r = TLSFingerprintRotator(
        "acct1", ["chrome120", "safari17_2", "edge101"],
        cooldown_secs=600, clock=clk,
    )
    r.rotate_on_failure(reason="429")
    pinned = r.get_current()
    rc1 = r._rotation_count

    # Same instant — cooldown not elapsed
    clk.advance(10)
    out = r.rotate_on_failure(reason="429-again")
    assert out == pinned
    assert r._rotation_count == rc1  # no advance


def test_rotate_after_cooldown_advances():
    clk = FakeClock()
    r = TLSFingerprintRotator(
        "acct1", ["chrome120", "safari17_2", "edge101"],
        cooldown_secs=600, clock=clk,
    )
    r.rotate_on_failure(reason="403")
    pin1 = r.get_current()
    clk.advance(601)
    out = r.rotate_on_failure(reason="429")
    assert out != pin1
    assert r._rotation_count == 2


def test_rotation_wraps_around():
    clk = FakeClock()
    pool = ["chrome120", "safari17_2"]
    r = TLSFingerprintRotator("acct1", pool, cooldown_secs=1, clock=clk)
    seen = {r.get_current()}
    for _ in range(4):
        clk.advance(2)
        seen.add(r.rotate_on_failure(reason="x"))
    assert seen == set(pool)


# ---- persistence (mocked DB) ------------------------------------------

class FakeConn:
    def __init__(self, row=None):
        self.row = row
        self.executed: list = []

    async def fetchrow(self, q, *args):
        return self.row

    async def execute(self, q, *args):
        self.executed.append((q, args))


class FakeAcquireCM:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, row=None):
        self.conn = FakeConn(row=row)

    def acquire(self):
        return FakeAcquireCM(self.conn)


@pytest.mark.asyncio
async def test_load_no_prior_row_persists_default():
    pool = FakePool(row=None)
    r = TLSFingerprintRotator("acct1", ["chrome120", "safari17_2"])
    found = await r.load(pool)
    assert found is False
    # Should have persisted the deterministic default
    assert len(pool.conn.executed) == 1
    q, args = pool.conn.executed[0]
    assert "INSERT INTO instagram_tls_state" in q
    assert args[0] == "acct1"
    assert args[1] == r.get_current()


@pytest.mark.asyncio
async def test_load_existing_row_restores_state():
    pool = FakePool(row={
        "account_id": "acct1",
        "impersonate_target": "safari17_2",
        "last_rotation_at": None,
        "rotation_count": 3,
        "last_failure_reason": "429",
    })
    r = TLSFingerprintRotator("acct1", ["chrome120", "safari17_2", "edge101"])
    found = await r.load(pool)
    assert found is True
    assert r.get_current() == "safari17_2"
    assert r._rotation_count == 3
    assert r._last_failure_reason == "429"


@pytest.mark.asyncio
async def test_persist_writes_current_state():
    pool = FakePool()
    r = TLSFingerprintRotator("acct1", ["chrome120", "safari17_2"])
    r.rotate_on_failure(reason="403")
    await r.persist(pool)
    q, args = pool.conn.executed[-1]
    assert "INSERT INTO instagram_tls_state" in q
    assert args[0] == "acct1"
    assert args[1] == r.get_current()
    assert args[2] == r._rotation_count
    assert args[3] == "403"


@pytest.mark.asyncio
async def test_load_with_none_pool_is_noop():
    r = TLSFingerprintRotator("acct1", ["chrome120"])
    assert await r.load(None) is False


def test_apply_row_drops_unknown_impersonate():
    r = TLSFingerprintRotator("acct1", ["chrome120", "safari17_2"])
    original = r.get_current()
    r.apply_row({"impersonate_target": "ie6", "rotation_count": 5})
    # unknown target ignored — index unchanged
    assert r.get_current() == original
    assert r._rotation_count == 5
