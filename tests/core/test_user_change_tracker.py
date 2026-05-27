"""Tests for src.core.user_change_tracker.

Coverage:
  1. AST parse — implicit (import success).
  2. No log when current is None (baseline observation).
  3. No log when fields unchanged.
  4. Log single change.
  5. Log multi-field diff in one call.
  6. Empty / None / "" in new payload is skipped (partial payload).
  7. None pool ⇒ no-op (returns 0).
  8. Unsafe identifier rejected (defence-in-depth).
  9. get_recent_changes round-trips a row dict.
 10. get_changes_by_field with and without since_ts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio

from src.core.user_change_tracker import (
    UserChangeTracker,
    TELEGRAM_TRACKED_FIELDS,
    INSTAGRAM_TRACKED_FIELDS,
    LEMON8_TRACKED_FIELDS,
    _is_safe_ident,
    _normalize,
)


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class FakeConn:
    def __init__(self, executed: list, fetched: list):
        self._executed = executed
        self._fetched = fetched

    async def execute(self, sql: str, *params: Any) -> str:
        self._executed.append((sql, params))
        return "INSERT 0 1"

    async def fetch(self, sql: str, *params: Any):
        self._fetched.append((sql, params))
        return self._fetch_rows

    def set_fetch_rows(self, rows):
        self._fetch_rows = rows


class FakeAcquire:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self):
        self.executed: list = []
        self.fetched: list = []
        self._conn = FakeConn(self.executed, self.fetched)
        self._conn.set_fetch_rows([])

    def acquire(self):
        return FakeAcquire(self._conn)

    def set_fetch_rows(self, rows):
        self._conn.set_fetch_rows(rows)


@pytest_asyncio.fixture
async def pool():
    return FakePool()


@pytest_asyncio.fixture
async def tracker(pool):
    return UserChangeTracker(pool)


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def test_normalize():
    assert _normalize(None) is None
    assert _normalize("") is None
    assert _normalize("hi") == "hi"
    assert _normalize(0) == "0"
    assert _normalize(False) == "False"


def test_is_safe_ident():
    assert _is_safe_ident("telegram_user_changes")
    assert _is_safe_ident("user_id")
    assert _is_safe_ident("a1_b2")
    assert not _is_safe_ident("")
    assert not _is_safe_ident("foo;DROP")
    assert not _is_safe_ident("foo bar")
    assert not _is_safe_ident("foo.bar")
    assert not _is_safe_ident(None)  # type: ignore[arg-type]


def test_telegram_tracked_fields_constant():
    # Sanity — original user_intelligence module tracked these five.
    for f in ("username", "first_name", "last_name", "bio", "profile_photo_id"):
        assert f in TELEGRAM_TRACKED_FIELDS


# ----------------------------------------------------------------------
# detect_and_log
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_log_when_current_is_none(tracker, pool):
    """First non-empty observation establishes baseline — no log."""
    n = await tracker.detect_and_log(
        table="telegram_user_changes",
        pk_col="user_id",
        pk_val=42,
        current_row=None,
        new_row={"username": "alice"},
        fields=["username"],
    )
    assert n == 0
    assert pool.executed == []


@pytest.mark.asyncio
async def test_no_log_when_unchanged(tracker, pool):
    n = await tracker.detect_and_log(
        table="telegram_user_changes",
        pk_col="user_id",
        pk_val=42,
        current_row={"username": "alice", "bio": "hi"},
        new_row={"username": "alice", "bio": "hi"},
        fields=["username", "bio"],
    )
    assert n == 0
    assert pool.executed == []


@pytest.mark.asyncio
async def test_log_single_change(tracker, pool):
    n = await tracker.detect_and_log(
        table="telegram_user_changes",
        pk_col="user_id",
        pk_val=42,
        current_row={"username": "alice", "bio": "hi"},
        new_row={"username": "alice2", "bio": "hi"},
        fields=["username", "bio"],
    )
    assert n == 1
    assert len(pool.executed) == 1
    sql, params = pool.executed[0]
    assert "INSERT INTO telegram_user_changes" in sql
    assert params == (42, "username", "alice", "alice2")


@pytest.mark.asyncio
async def test_log_multi_field_diff(tracker, pool):
    n = await tracker.detect_and_log(
        table="telegram_user_changes",
        pk_col="user_id",
        pk_val=7,
        current_row={"username": "old_u", "first_name": "Old", "bio": "same"},
        new_row={"username": "new_u", "first_name": "New", "bio": "same"},
        fields=["username", "first_name", "bio"],
    )
    assert n == 2
    assert len(pool.executed) == 2
    fields_logged = sorted(p[1] for _, p in pool.executed)
    assert fields_logged == ["first_name", "username"]


@pytest.mark.asyncio
async def test_partial_payload_skipped(tracker, pool):
    """If new_row is missing a field (or it's empty), don't log a transition
    from known→None."""
    n = await tracker.detect_and_log(
        table="telegram_user_changes",
        pk_col="user_id",
        pk_val=1,
        current_row={"username": "alice", "bio": "hello"},
        new_row={"username": "alice", "bio": ""},  # empty string == None
        fields=["username", "bio"],
    )
    assert n == 0
    assert pool.executed == []


@pytest.mark.asyncio
async def test_attribute_access_on_current_row(tracker, pool):
    class Row:
        username = "alice"
        bio = "hello"

    n = await tracker.detect_and_log(
        table="telegram_user_changes",
        pk_col="user_id",
        pk_val=1,
        current_row=Row(),
        new_row={"username": "alice2", "bio": "hello"},
        fields=["username", "bio"],
    )
    assert n == 1


@pytest.mark.asyncio
async def test_none_pool_is_noop():
    t = UserChangeTracker(pool=None)
    n = await t.detect_and_log(
        table="telegram_user_changes",
        pk_col="user_id",
        pk_val=1,
        current_row={"username": "a"},
        new_row={"username": "b"},
        fields=["username"],
    )
    assert n == 0


@pytest.mark.asyncio
async def test_unsafe_table_rejected(tracker, pool):
    n = await tracker.detect_and_log(
        table="telegram_user_changes; DROP TABLE foo",
        pk_col="user_id",
        pk_val=1,
        current_row={"username": "a"},
        new_row={"username": "b"},
        fields=["username"],
    )
    assert n == 0
    assert pool.executed == []


@pytest.mark.asyncio
async def test_insert_failure_swallowed(tracker, pool):
    async def boom(*a, **kw):
        raise RuntimeError("DB blown up")
    pool._conn.execute = boom  # type: ignore[assignment]

    n = await tracker.detect_and_log(
        table="telegram_user_changes",
        pk_col="user_id",
        pk_val=1,
        current_row={"username": "a"},
        new_row={"username": "b"},
        fields=["username"],
    )
    # Failure was logged, not raised.
    assert n == 0


# ----------------------------------------------------------------------
# Read path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_changes(tracker, pool):
    pool.set_fetch_rows([
        {"id": 1, "user_id": 42, "field": "username",
         "old_value": "a", "new_value": "b",
         "detected_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
    ])
    out = await tracker.get_recent_changes(42, limit=10)
    assert len(out) == 1
    assert out[0]["field"] == "username"
    sql, params = pool.fetched[0]
    assert "WHERE user_id = $1" in sql
    assert params == (42, 10)


@pytest.mark.asyncio
async def test_get_changes_by_field_no_since(tracker, pool):
    pool.set_fetch_rows([])
    out = await tracker.get_changes_by_field("username", limit=25)
    assert out == []
    sql, params = pool.fetched[0]
    assert "WHERE field = $1" in sql
    assert "detected_at >=" not in sql
    assert params == ("username", 25)


@pytest.mark.asyncio
async def test_get_changes_by_field_with_since(tracker, pool):
    pool.set_fetch_rows([])
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await tracker.get_changes_by_field("username", since_ts=ts, limit=5)
    sql, params = pool.fetched[0]
    assert "detected_at >= $2" in sql
    assert params == ("username", ts, 5)


@pytest.mark.asyncio
async def test_get_recent_changes_none_pool():
    t = UserChangeTracker(pool=None)
    assert await t.get_recent_changes(1) == []
    assert await t.get_changes_by_field("username") == []


# ----------------------------------------------------------------------
# Platform-specific tracked-field constants + multi-table dispatch
# ----------------------------------------------------------------------


def test_instagram_tracked_fields_constant():
    expected = {
        "username", "full_name", "biography",
        "is_verified", "is_private", "is_business",
        "profile_pic_url",
        "follower_count", "following_count", "post_count",
        "external_url",
    }
    assert set(INSTAGRAM_TRACKED_FIELDS) == expected
    # Tuple — immutable, safe to share across callers.
    assert isinstance(INSTAGRAM_TRACKED_FIELDS, tuple)


def test_lemon8_tracked_fields_constant():
    expected = {
        "username", "nickname", "biography",
        "follower_count", "following_count", "like_count", "post_count",
        "profile_pic_url", "region",
    }
    assert set(LEMON8_TRACKED_FIELDS) == expected
    assert isinstance(LEMON8_TRACKED_FIELDS, tuple)


@pytest.mark.asyncio
async def test_detect_and_log_instagram_table(tracker, pool):
    """Smoke-test routing into the instagram_user_changes table with the
    INSTAGRAM_TRACKED_FIELDS tuple."""
    n = await tracker.detect_and_log(
        table="instagram_user_changes",
        pk_col="user_id",
        pk_val=12345,
        current_row={
            "username": "alice", "full_name": "Alice", "biography": "old bio",
            "follower_count": 100, "is_verified": False,
        },
        new_row={
            "username": "alice", "full_name": "Alice", "biography": "new bio",
            "follower_count": 105, "is_verified": True,
        },
        fields=INSTAGRAM_TRACKED_FIELDS,
    )
    # biography, follower_count, is_verified all changed = 3 rows.
    assert n == 3
    assert len(pool.executed) == 3
    for sql, _ in pool.executed:
        assert "INSERT INTO instagram_user_changes" in sql
        assert "(user_id, field, old_value, new_value)" in sql
    fields_logged = sorted(p[1] for _, p in pool.executed)
    assert fields_logged == ["biography", "follower_count", "is_verified"]


@pytest.mark.asyncio
async def test_detect_and_log_instagram_partial_payload_skipped(tracker, pool):
    """IG edge fields like follower_count routinely missing on partial fetches
    must NOT log a known→None transition."""
    n = await tracker.detect_and_log(
        table="instagram_user_changes",
        pk_col="user_id",
        pk_val=42,
        current_row={"username": "alice", "follower_count": 1000},
        new_row={"username": "alice"},  # follower_count absent — partial
        fields=INSTAGRAM_TRACKED_FIELDS,
    )
    assert n == 0
    assert pool.executed == []


@pytest.mark.asyncio
async def test_detect_and_log_lemon8_table(tracker, pool):
    """Smoke-test routing into the lemon8_user_changes table with a string pk
    (lemon8 platform_user_id is opaque/string-shaped) and LEMON8_TRACKED_FIELDS."""
    n = await tracker.detect_and_log(
        table="lemon8_user_changes",
        pk_col="user_id",
        pk_val="lemon8_user_abc123",
        current_row={
            "username": "creator", "nickname": "Creator",
            "biography": "old", "follower_count": 50, "region": "JP",
        },
        new_row={
            "username": "creator", "nickname": "Creator!",
            "biography": "old", "follower_count": 50, "region": "US",
        },
        fields=LEMON8_TRACKED_FIELDS,
    )
    # nickname + region changed = 2 rows.
    assert n == 2
    fields_logged = sorted(p[1] for _, p in pool.executed)
    assert fields_logged == ["nickname", "region"]
    for sql, params in pool.executed:
        assert "INSERT INTO lemon8_user_changes" in sql
        assert params[0] == "lemon8_user_abc123"


@pytest.mark.asyncio
async def test_detect_and_log_lemon8_baseline_no_log(tracker, pool):
    """First observation (no prior row) is baseline-only on lemon8 too."""
    n = await tracker.detect_and_log(
        table="lemon8_user_changes",
        pk_col="user_id",
        pk_val="brand_new_user",
        current_row=None,
        new_row={
            "username": "newbie", "nickname": "Newbie",
            "follower_count": 0, "like_count": 0,
        },
        fields=LEMON8_TRACKED_FIELDS,
    )
    assert n == 0
    assert pool.executed == []


@pytest.mark.asyncio
async def test_tracked_fields_constants_are_disjoint_enough():
    """Sanity: each platform's field tuple has the platform's distinguishing
    columns. Catches accidental copy-paste of TELEGRAM_TRACKED_FIELDS."""
    assert "first_name" in TELEGRAM_TRACKED_FIELDS
    assert "first_name" not in INSTAGRAM_TRACKED_FIELDS
    assert "first_name" not in LEMON8_TRACKED_FIELDS
    assert "biography" in INSTAGRAM_TRACKED_FIELDS
    assert "biography" in LEMON8_TRACKED_FIELDS
    assert "biography" not in TELEGRAM_TRACKED_FIELDS  # telegram uses "bio"
    assert "region" in LEMON8_TRACKED_FIELDS
    assert "region" not in INSTAGRAM_TRACKED_FIELDS
    assert "external_url" in INSTAGRAM_TRACKED_FIELDS
    assert "external_url" not in LEMON8_TRACKED_FIELDS
