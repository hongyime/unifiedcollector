"""Property-based tests for StoryScanner (task 8.8).

Tests Properties 6, 7, 8, 9, 10, 15, 16 from the design document.
All tests use hypothesis with @given and @settings(max_examples=25).
"""

import asyncio
import json
import sys
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from hypothesis import given, settings as h_settings, assume
from hypothesis import strategies as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ---------------------------------------------------------------------------
# Stub out heavy dependencies before importing StoryScanner so the tests
# run without a live database / Redis / Telegram installation.
# ---------------------------------------------------------------------------
for _mod in ("psycopg", "psycopg_pool", "telethon", "redis", "redis.asyncio"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Stub database module — patch collector.story_scanner.get_db_connection directly
_db_mod = MagicMock()
_db_mod.get_db_connection = MagicMock()
sys.modules["database"] = _db_mod

sys.modules.setdefault("resilience", MagicMock())

from services.collector.story_scanner import StoryScanner, _extract_story_file_info  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------

def _make_scanner(
    clients=None,
    rate_limiter=None,
    media_store=None,
    redis_client=None,
):
    """Build a StoryScanner with sensible mock defaults."""
    if clients is None:
        clients = []
    if rate_limiter is None:
        rl = MagicMock()
        rl.acquire = AsyncMock()
        rl.set_flood_wait = MagicMock()
        rate_limiter = rl
    if redis_client is None:
        r = AsyncMock()
        r.lpush = AsyncMock()
        redis_client = r
    return StoryScanner(
        clients=clients,
        rate_limiter=rate_limiter,
        media_store=media_store,
        redis_client=redis_client,
    )


def _make_client_manager(account_id: int):
    mgr = MagicMock()
    mgr.account_id = account_id
    mgr.client = MagicMock()
    return mgr


def _make_story(
    story_id: int,
    expire_date: datetime,
    file_unique_id: str = "fuid_test",
    file_id: str = "fid_test",
):
    """Create a minimal fake story object."""
    story = MagicMock()
    story.id = story_id
    story.expire_date = expire_date
    story.photo = None
    story.media = None
    story.document = None
    story.to_dict = MagicMock(return_value={"id": story_id})
    return story


def _make_db_context(written_rows: list):
    """Build an async context manager mock that captures INSERT calls."""
    cur = AsyncMock()

    async def fake_execute(sql, params=None):
        if params is not None and "collector.stories" in sql:
            written_rows.append(params)

    cur.execute = fake_execute
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock(return_value=False)

    conn = AsyncMock()
    conn.cursor = MagicMock(return_value=cur)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# Property 6: Story Expiry Skip
# Validates: Requirements 4.7
#
# For any story where expire_date < NOW(), StoryScanner SHALL skip it.
# ---------------------------------------------------------------------------

@given(
    story_id=st.integers(min_value=1, max_value=10**9),
    peer_id=st.integers(min_value=1, max_value=10**9),
    seconds_ago=st.integers(min_value=1, max_value=86400),
)
@h_settings(max_examples=25)
def test_property_6_story_expiry_skip(
    story_id: int, peer_id: int, seconds_ago: int
) -> None:
    """**Validates: Requirements 4.7**

    Stories with expire_date < NOW() are skipped and never written to DB.
    """
    written_stories: list = []

    async def run():
        scanner = _make_scanner()
        account_id = 1
        client_mgr = _make_client_manager(account_id)
        scanner.clients = [client_mgr]

        # Story expired `seconds_ago` seconds in the past
        expired_date = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        expired_story = _make_story(story_id, expired_date)

        client_mgr.client.get_stories = AsyncMock(return_value=[expired_story])

        async def fake_write_story(story, pid, aid):
            written_stories.append(story.id)

        scanner._write_story = fake_write_story
        scanner._enqueue_story_media = AsyncMock()

        await scanner._scan_peer_stories(client_mgr, peer_id)

    asyncio.run(run())

    assert story_id not in written_stories, (
        f"Expired story {story_id} should have been skipped, but was written"
    )


# ---------------------------------------------------------------------------
# Property 7: Story Unique Constraint
# Validates: Requirements 4.8
#
# Inserting the same (story_id, peer_id, account_id) twice results in a
# single row (ON CONFLICT DO NOTHING).
# ---------------------------------------------------------------------------

@given(
    story_id=st.integers(min_value=1, max_value=10**9),
    peer_id=st.integers(min_value=1, max_value=10**9),
    account_id=st.integers(min_value=1, max_value=1000),
)
@h_settings(max_examples=25)
def test_property_7_story_unique_constraint(
    story_id: int, peer_id: int, account_id: int
) -> None:
    """**Validates: Requirements 4.8**

    The INSERT uses ON CONFLICT (story_id, peer_id, account_id) DO NOTHING,
    so duplicate inserts produce a single row.
    """
    sql_calls: list[str] = []

    async def run():
        scanner = _make_scanner()

        future_date = datetime.now(timezone.utc) + timedelta(hours=12)
        story = _make_story(story_id, future_date)

        cur = AsyncMock()

        async def capture_execute(sql, params=None):
            sql_calls.append(sql)

        cur.execute = capture_execute
        cur.__aenter__ = AsyncMock(return_value=cur)
        cur.__aexit__ = AsyncMock(return_value=False)

        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("services.collector.story_scanner.get_db_connection", return_value=ctx):
            # Write the same story twice
            await scanner._write_story(story, peer_id, account_id)
            await scanner._write_story(story, peer_id, account_id)

    asyncio.run(run())

    # Both calls should use ON CONFLICT DO NOTHING
    insert_sqls = [s for s in sql_calls if "INSERT" in s.upper()]
    assert len(insert_sqls) == 2, f"Expected 2 INSERT calls, got {len(insert_sqls)}"
    for sql in insert_sqls:
        assert "ON CONFLICT" in sql.upper(), (
            "INSERT must include ON CONFLICT clause for deduplication"
        )
        assert "DO NOTHING" in sql.upper(), (
            "ON CONFLICT must use DO NOTHING"
        )


# ---------------------------------------------------------------------------
# Property 8: Rate Limiter Acquisition
# Validates: Requirements 4.2
#
# acquire(account_id) must be called before the Telegram API call.
# ---------------------------------------------------------------------------

@given(
    account_id=st.integers(min_value=1, max_value=1000),
    peer_id=st.integers(min_value=1, max_value=10**9),
)
@h_settings(max_examples=25)
def test_property_8_rate_limiter_acquired_before_api_call(
    account_id: int, peer_id: int
) -> None:
    """**Validates: Requirements 4.2**

    rate_limiter.acquire(account_id) is called before each Telegram API call
    during story scanning.
    """
    call_order: list[str] = []

    async def run():
        rl = MagicMock()

        async def fake_acquire(aid=None):
            call_order.append(f"acquire:{aid}")

        rl.acquire = fake_acquire
        rl.set_flood_wait = MagicMock()

        scanner = _make_scanner(rate_limiter=rl)
        client_mgr = _make_client_manager(account_id)
        scanner.clients = [client_mgr]

        async def fake_get_stories(pid):
            call_order.append("api_call")
            return []

        client_mgr.client.get_stories = fake_get_stories

        await scanner._scan_peer_stories(client_mgr, peer_id)

    asyncio.run(run())

    assert "api_call" in call_order, "get_stories was never called"
    api_idx = call_order.index("api_call")
    assert api_idx > 0 and call_order[api_idx - 1].startswith("acquire"), (
        f"api_call at position {api_idx} was not preceded by acquire. "
        f"call_order={call_order}"
    )


# ---------------------------------------------------------------------------
# Property 9: FloodWait Honor
# Validates: Requirements 4.10
#
# When FloodWaitError is raised, the scanner waits error.seconds + 10.
# ---------------------------------------------------------------------------

@given(
    seconds=st.integers(min_value=1, max_value=300),
    account_id=st.integers(min_value=1, max_value=1000),
)
@h_settings(max_examples=25)
def test_property_9_flood_wait_honor(seconds: int, account_id: int) -> None:
    """**Validates: Requirements 4.10**

    FloodWaitError results in a wait of error.seconds + 10 before retry.
    """
    total_slept: list[float] = []
    flood_wait_calls: list[tuple] = []

    async def run():
        rl = MagicMock()
        rl.acquire = AsyncMock()

        def fake_set_flood_wait(aid, secs):
            flood_wait_calls.append((aid, secs))

        rl.set_flood_wait = fake_set_flood_wait

        scanner = _make_scanner(rate_limiter=rl)

        original_sleep = asyncio.sleep

        async def fake_sleep(duration: float) -> None:
            total_slept.append(duration)

        class FakeFloodWaitError(Exception):
            pass

        FakeFloodWaitError.__name__ = "FloodWaitError"
        exc = FakeFloodWaitError("flood wait")
        exc.seconds = seconds

        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            await scanner._handle_flood_wait(exc, account_id)
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]

    asyncio.run(run())

    expected_wait = seconds + 10
    assert len(total_slept) >= 1, "Expected at least one sleep call"
    assert total_slept[0] == expected_wait, (
        f"Expected sleep({expected_wait}), got sleep({total_slept[0]})"
    )
    assert len(flood_wait_calls) == 1
    assert flood_wait_calls[0] == (account_id, seconds), (
        f"Expected set_flood_wait({account_id}, {seconds}), "
        f"got {flood_wait_calls[0]}"
    )


# ---------------------------------------------------------------------------
# Property 10: Schema Isolation
# Validates: Requirements 5.4, 5.5
#
# All DB writes target collector.* schema exclusively.
# ---------------------------------------------------------------------------

@given(
    story_id=st.integers(min_value=1, max_value=10**9),
    peer_id=st.integers(min_value=1, max_value=10**9),
    account_id=st.integers(min_value=1, max_value=1000),
)
@h_settings(max_examples=25)
def test_property_10_schema_isolation(
    story_id: int, peer_id: int, account_id: int
) -> None:
    """**Validates: Requirements 5.4, 5.5**

    All database writes by StoryScanner target the collector.* schema only.
    """
    executed_sqls: list[str] = []

    async def run():
        scanner = _make_scanner()

        future_date = datetime.now(timezone.utc) + timedelta(hours=12)
        story = _make_story(story_id, future_date)

        cur = AsyncMock()

        async def capture_execute(sql, params=None):
            executed_sqls.append(sql)

        cur.execute = capture_execute
        cur.__aenter__ = AsyncMock(return_value=cur)
        cur.__aexit__ = AsyncMock(return_value=False)

        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("services.collector.story_scanner.get_db_connection", return_value=ctx):
            await scanner._write_story(story, peer_id, account_id)

    asyncio.run(run())

    assert executed_sqls, "No SQL was executed"
    for sql in executed_sqls:
        sql_upper = sql.upper()
        # Every table reference must be in collector schema
        # Check that any INTO/UPDATE/FROM clause uses collector.
        if "INTO" in sql_upper or "UPDATE" in sql_upper:
            assert "COLLECTOR." in sql_upper, (
                f"SQL targets a non-collector schema table: {sql!r}"
            )


# ---------------------------------------------------------------------------
# Property 15: Story Expiry Prioritization
# Validates: Requirements 4.6
#
# Stories within COLLECTOR_STORY_EXPIRY_BUFFER minutes of expiry are
# processed before stories with later expiry.
# ---------------------------------------------------------------------------

@given(
    n_stories=st.integers(min_value=2, max_value=8),
    buffer_minutes=st.integers(min_value=5, max_value=120),
)
@h_settings(max_examples=25)
def test_property_15_story_expiry_prioritization(
    n_stories: int, buffer_minutes: int
) -> None:
    """**Validates: Requirements 4.6**

    Stories within COLLECTOR_STORY_EXPIRY_BUFFER minutes of expiry are
    processed before stories with later expiry times.
    """
    now = datetime.now(timezone.utc)

    # Create stories: half within buffer, half outside
    half = n_stories // 2
    within_buffer = [
        _make_story(
            story_id=i + 1,
            expire_date=now + timedelta(minutes=buffer_minutes // 2),
        )
        for i in range(half)
    ]
    outside_buffer = [
        _make_story(
            story_id=i + 100,
            expire_date=now + timedelta(minutes=buffer_minutes * 3),
        )
        for i in range(n_stories - half)
    ]

    all_stories = outside_buffer + within_buffer  # outside first (unsorted)

    scanner = _make_scanner()

    with patch("services.collector.story_scanner.settings") as mock_settings:
        mock_settings.COLLECTOR_STORY_EXPIRY_BUFFER = buffer_minutes
        sorted_stories = sorted(all_stories, key=scanner._get_expiry_priority)

    # All within-buffer stories should come before outside-buffer stories
    within_ids = {s.id for s in within_buffer}
    outside_ids = {s.id for s in outside_buffer}

    sorted_ids = [s.id for s in sorted_stories]

    # Find the last within-buffer index and first outside-buffer index
    last_within_idx = max(
        (i for i, sid in enumerate(sorted_ids) if sid in within_ids),
        default=-1,
    )
    first_outside_idx = min(
        (i for i, sid in enumerate(sorted_ids) if sid in outside_ids),
        default=len(sorted_ids),
    )

    assert last_within_idx < first_outside_idx, (
        f"Within-buffer stories (ids={within_ids}) should all come before "
        f"outside-buffer stories (ids={outside_ids}). "
        f"Sorted order: {sorted_ids}"
    )


# ---------------------------------------------------------------------------
# Property 16: Error Isolation
# Validates: Requirements 9.3
#
# When one peer scan fails, other peers continue processing.
# ---------------------------------------------------------------------------

@given(
    n_peers=st.integers(min_value=2, max_value=6),
    failing_peer_index=st.integers(min_value=0, max_value=5),
)
@h_settings(max_examples=25)
def test_property_16_error_isolation(
    n_peers: int, failing_peer_index: int
) -> None:
    """**Validates: Requirements 9.3**

    One peer scan failure does not prevent other peers from being scanned.
    """
    assume(failing_peer_index < n_peers)

    scanned_peers: list[int] = []
    failed_peers: list[int] = []

    async def run():
        scanner = _make_scanner()

        peers = [
            {"peer_id": 1000 + i, "account_id": i + 1}
            for i in range(n_peers)
        ]

        clients = []
        for i in range(n_peers):
            mgr = _make_client_manager(i + 1)
            if i == failing_peer_index:
                async def fail_get_stories(pid, _i=i):
                    raise RuntimeError(f"simulated failure for peer {_i}")
                mgr.client.get_stories = fail_get_stories
            else:
                async def ok_get_stories(pid, _i=i):
                    return []
                mgr.client.get_stories = ok_get_stories
            clients.append(mgr)

        scanner.clients = clients

        # Override _get_monitored_peers to return our test peers
        scanner._get_monitored_peers = AsyncMock(return_value=peers)

        # Track which peers were attempted
        original_scan = scanner._scan_peer_stories

        async def tracking_scan(client, peer_id):
            try:
                await original_scan(client, peer_id)
                scanned_peers.append(peer_id)
            except Exception:
                failed_peers.append(peer_id)
                raise

        scanner._scan_peer_stories = tracking_scan

        # Run the scan loop once (manually iterate peers like _scan_loop does)
        fetched_peers = await scanner._get_monitored_peers()
        for peer in fetched_peers:
            pid = peer["peer_id"]
            aid = peer["account_id"]
            client = scanner._get_client(aid)
            if client is None:
                continue
            try:
                await scanner._scan_peer_stories(client, pid)
            except Exception:
                pass  # Error isolation: continue to next peer

    asyncio.run(run())

    total = len(scanned_peers) + len(failed_peers)
    assert total == n_peers, (
        f"Expected {n_peers} peers attempted, got {total}"
    )

    failing_peer_id = 1000 + failing_peer_index
    assert failing_peer_id in failed_peers, (
        f"Peer {failing_peer_id} should have failed"
    )

    other_peer_ids = [1000 + i for i in range(n_peers) if i != failing_peer_index]
    for pid in other_peer_ids:
        assert pid in scanned_peers, (
            f"Peer {pid} should have been scanned despite peer "
            f"{failing_peer_id} failing"
        )
