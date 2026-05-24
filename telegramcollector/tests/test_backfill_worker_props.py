"""Property-based tests for BackfillWorker (task 4.9).

Tests Properties 1, 2, 3, 8, 9, 11, 12, 13, 16 from the design document.
All tests use hypothesis with @given and @settings(max_examples=25).
"""

import asyncio
import json
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from hypothesis import given, settings as h_settings, assume
from hypothesis import strategies as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ---------------------------------------------------------------------------
# Stub out heavy dependencies before importing BackfillWorker so the tests
# run without a live database / Redis / Telegram installation.
# ---------------------------------------------------------------------------
for _mod in ("psycopg", "psycopg_pool", "telethon", "redis", "redis.asyncio"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Stub database module
_db_mod = MagicMock()
_db_mod.get_db_connection = MagicMock()
sys.modules["database"] = _db_mod

# Stub resilience module
sys.modules.setdefault("resilience", MagicMock())

from services.collector.backfill_worker import BackfillWorker, _retry_with_backoff  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------

def _make_worker(
    clients=None,
    rate_limiter=None,
    media_store=None,
    redis_client=None,
):
    """Build a BackfillWorker with sensible mock defaults."""
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
    return BackfillWorker(
        clients=clients,
        rate_limiter=rate_limiter,
        media_store=media_store,
        redis_client=redis_client,
    )


def _make_message(msg_id: int, has_media: bool = False, sender_id: int = 1):
    """Create a minimal fake Telegram message object."""
    msg = MagicMock()
    msg.id = msg_id
    msg.sender_id = sender_id
    msg.sender = None
    msg.media = MagicMock() if has_media else None
    msg.fwd_from = None
    msg.reply_to = None
    msg.views = None
    msg.photo = None
    msg.video = None
    msg.audio = None
    msg.voice = None
    msg.document = None
    msg.sticker = None
    msg.poll = None
    msg.geo = None
    msg.geo_live = None
    msg.contact = None
    msg.action = None
    msg.to_dict = MagicMock(return_value={"id": msg_id})
    return msg


def _make_job(job_id: int, chat_id: int, account_id: int, status: str = "pending"):
    return {
        "id": job_id,
        "chat_id": chat_id,
        "account_id": account_id,
        "status": status,
    }


def _make_client_manager(account_id: int):
    mgr = MagicMock()
    mgr.account_id = account_id
    mgr.client = MagicMock()
    return mgr


# ---------------------------------------------------------------------------
# Property 1: Backfill Progress Persistence
# Validates: Requirements 2.6
#
# When a backfill job has an existing last_processed_message_id, the first
# API call must use that value as the cursor (max_id), not 0.
# ---------------------------------------------------------------------------

@given(
    last_msg_id=st.integers(min_value=1, max_value=10**9),
    chat_id=st.integers(min_value=1, max_value=10**9),
    account_id=st.integers(min_value=1, max_value=1000),
)
@h_settings(max_examples=25)
def test_property_1_backfill_resumes_from_checkpoint(
    last_msg_id: int, chat_id: int, account_id: int
) -> None:
    """**Validates: Requirements 2.6**

    BackfillWorker resumes from last_processed_message_id rather than
    fetching from the latest message when a checkpoint exists.
    """
    captured_max_ids: list[int] = []

    async def run():
        worker = _make_worker()
        client_mgr = _make_client_manager(account_id)
        worker.clients = [client_mgr]

        # Simulate iter_messages capturing the max_id argument
        async def fake_iter_messages(entity, limit, max_id, reverse):
            captured_max_ids.append(max_id)
            return
            yield  # make it an async generator

        client_mgr.client.iter_messages = fake_iter_messages

        # Patch _get_or_create_backfill_state to return last_msg_id
        worker._get_or_create_backfill_state = AsyncMock(return_value=last_msg_id)
        worker._mark_job_in_progress = AsyncMock()
        worker._mark_job_completed = AsyncMock()
        worker._update_backfill_state = AsyncMock()

        job = _make_job(1, chat_id, account_id)
        await worker._process_job(job)

    asyncio.run(run())

    # The first batch call must use last_msg_id as the cursor
    assert len(captured_max_ids) >= 1
    assert captured_max_ids[0] == last_msg_id, (
        f"Expected first max_id={last_msg_id}, got {captured_max_ids[0]}"
    )


# ---------------------------------------------------------------------------
# Property 2: Backfill State Transition
# Validates: Requirements 2.3, 2.4, 2.5
#
# For any job, the final status in backfill_state must be 'completed' on
# success or 'failed' on error — never left as 'in_progress'.
# ---------------------------------------------------------------------------

@given(
    chat_id=st.integers(min_value=1, max_value=10**9),
    account_id=st.integers(min_value=1, max_value=1000),
    should_fail=st.booleans(),
)
@h_settings(max_examples=25)
def test_property_2_backfill_state_transitions(
    chat_id: int, account_id: int, should_fail: bool
) -> None:
    """**Validates: Requirements 2.3, 2.4, 2.5**

    Backfill job state transitions: pending → in_progress → completed/failed.
    The final state is never 'in_progress'.
    """
    state_updates: list[dict] = []

    async def run():
        worker = _make_worker()
        client_mgr = _make_client_manager(account_id)
        worker.clients = [client_mgr]

        # Empty message batch → job completes immediately
        async def fake_iter_messages(entity, limit, max_id, reverse):
            if should_fail:
                raise RuntimeError("simulated failure")
            return
            yield

        client_mgr.client.iter_messages = fake_iter_messages
        worker._get_or_create_backfill_state = AsyncMock(return_value=0)
        worker._mark_job_in_progress = AsyncMock()
        worker._mark_job_completed = AsyncMock()
        worker._mark_job_failed = AsyncMock()

        async def capture_state_update(c, a, **fields):
            state_updates.append(fields)

        worker._update_backfill_state = capture_state_update

        job = _make_job(1, chat_id, account_id)
        await worker._process_job(job)

    asyncio.run(run())

    # Extract final status from state updates
    final_statuses = [u["status"] for u in state_updates if "status" in u]
    assert final_statuses, "Expected at least one status update"
    final = final_statuses[-1]
    assert final in ("completed", "failed"), (
        f"Final status must be 'completed' or 'failed', got '{final}'"
    )
    if should_fail:
        assert final == "failed"
    else:
        assert final == "completed"


# ---------------------------------------------------------------------------
# Property 3: Message Write Before Media Enqueue
# Validates: Requirements 1.4, 1.5
#
# For any message with media, _write_message must be called before
# _enqueue_media (DB write before LPUSH).
# ---------------------------------------------------------------------------

@given(
    message_id=st.integers(min_value=1, max_value=10**9),
    chat_id=st.integers(min_value=1, max_value=10**9),
)
@h_settings(max_examples=25)
def test_property_3_message_write_before_media_enqueue(
    message_id: int, chat_id: int
) -> None:
    """**Validates: Requirements 1.4, 1.5**

    DB write (_write_message) happens before media queue enqueue (_enqueue_media).
    """
    call_order: list[str] = []

    async def run():
        worker = _make_worker()

        async def fake_write_message(msg, cid):
            call_order.append("write_message")

        async def fake_enqueue_media(msg, cid):
            call_order.append("enqueue_media")

        worker._write_message = fake_write_message
        worker._enqueue_media = fake_enqueue_media
        worker._upsert_user = AsyncMock()
        worker._write_user_sighting = AsyncMock()

        msg = _make_message(message_id, has_media=True)
        account_id = 1
        client_mgr = _make_client_manager(account_id)
        worker.clients = [client_mgr]

        async def fake_iter_messages(entity, limit, max_id, reverse):
            yield msg

        client_mgr.client.iter_messages = fake_iter_messages
        worker._get_or_create_backfill_state = AsyncMock(return_value=0)
        worker._mark_job_in_progress = AsyncMock()
        worker._mark_job_completed = AsyncMock()
        worker._update_backfill_state = AsyncMock()

        job = _make_job(1, chat_id, account_id)
        await worker._process_job(job)

    asyncio.run(run())

    assert "write_message" in call_order, "write_message was never called"
    assert "enqueue_media" in call_order, "enqueue_media was never called"
    write_idx = call_order.index("write_message")
    enqueue_idx = call_order.index("enqueue_media")
    assert write_idx < enqueue_idx, (
        f"write_message (pos {write_idx}) must come before "
        f"enqueue_media (pos {enqueue_idx})"
    )


# ---------------------------------------------------------------------------
# Property 8: Rate Limiter Acquisition
# Validates: Requirements 1.3
#
# acquire(account_id) must be called before every Telegram API call.
# ---------------------------------------------------------------------------

@given(
    account_id=st.integers(min_value=1, max_value=1000),
    chat_id=st.integers(min_value=1, max_value=10**9),
    n_messages=st.integers(min_value=0, max_value=5),
)
@h_settings(max_examples=25)
def test_property_8_rate_limiter_acquired_before_api_call(
    account_id: int, chat_id: int, n_messages: int
) -> None:
    """**Validates: Requirements 1.3**

    rate_limiter.acquire(account_id) is called before each Telegram API call
    during backfill.
    """
    call_order: list[str] = []

    async def run():
        rl = MagicMock()

        async def fake_acquire(aid=None):
            call_order.append(f"acquire:{aid}")

        rl.acquire = fake_acquire
        rl.set_flood_wait = MagicMock()

        worker = _make_worker(rate_limiter=rl)
        client_mgr = _make_client_manager(account_id)
        worker.clients = [client_mgr]

        messages = [_make_message(i + 1) for i in range(n_messages)]

        async def fake_iter_messages(entity, limit, max_id, reverse):
            call_order.append("api_call")
            for m in messages:
                yield m

        client_mgr.client.iter_messages = fake_iter_messages
        worker._get_or_create_backfill_state = AsyncMock(return_value=0)
        worker._mark_job_in_progress = AsyncMock()
        worker._mark_job_completed = AsyncMock()
        worker._update_backfill_state = AsyncMock()
        worker._write_message = AsyncMock()
        worker._enqueue_media = AsyncMock()
        worker._upsert_user = AsyncMock()
        worker._write_user_sighting = AsyncMock()

        job = _make_job(1, chat_id, account_id)
        await worker._process_job(job)

    asyncio.run(run())

    # Every "api_call" must be preceded by an "acquire"
    for i, event in enumerate(call_order):
        if event == "api_call":
            assert i > 0 and call_order[i - 1].startswith("acquire"), (
                f"api_call at position {i} was not preceded by acquire. "
                f"call_order={call_order}"
            )


# ---------------------------------------------------------------------------
# Property 9: FloodWait Honor
# Validates: Requirements 1.12
#
# When FloodWaitError is raised, the worker waits error.seconds + 10 before
# retrying.
# ---------------------------------------------------------------------------

@given(
    seconds=st.integers(min_value=1, max_value=300),
    account_id=st.integers(min_value=1, max_value=1000),
)
@h_settings(max_examples=25)
def test_property_9_flood_wait_honor(seconds: int, account_id: int) -> None:
    """**Validates: Requirements 1.12**

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

        worker = _make_worker(rate_limiter=rl)

        original_sleep = asyncio.sleep

        async def fake_sleep(duration: float) -> None:
            total_slept.append(duration)

        # Create a FloodWaitError-like exception
        class FakeFloodWaitError(Exception):
            pass

        FakeFloodWaitError.__name__ = "FloodWaitError"
        exc = FakeFloodWaitError("flood wait")
        exc.seconds = seconds

        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            await worker._handle_flood_wait(exc, account_id)
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]

    asyncio.run(run())

    expected_wait = seconds + 10
    assert len(total_slept) >= 1, "Expected at least one sleep call"
    assert total_slept[0] == expected_wait, (
        f"Expected sleep({expected_wait}), got sleep({total_slept[0]})"
    )
    # rate_limiter.set_flood_wait must be called with the original seconds
    assert len(flood_wait_calls) == 1
    assert flood_wait_calls[0] == (account_id, seconds), (
        f"Expected set_flood_wait({account_id}, {seconds}), "
        f"got {flood_wait_calls[0]}"
    )


# ---------------------------------------------------------------------------
# Property 11: Backfill Cursor Order
# Validates: Requirements 1.2
#
# Messages are fetched in descending order (newest to oldest) — iter_messages
# is called with reverse=False.
# ---------------------------------------------------------------------------

@given(
    message_ids=st.lists(
        st.integers(min_value=1, max_value=10**9),
        min_size=2,
        max_size=20,
        unique=True,
    ),
    chat_id=st.integers(min_value=1, max_value=10**9),
)
@h_settings(max_examples=25)
def test_property_11_backfill_cursor_order(
    message_ids: list[int], chat_id: int
) -> None:
    """**Validates: Requirements 1.2**

    Messages are fetched in descending order (newest to oldest).
    iter_messages is called with reverse=False.
    """
    iter_kwargs: list[dict] = []

    async def run():
        account_id = 1
        worker = _make_worker()
        client_mgr = _make_client_manager(account_id)
        worker.clients = [client_mgr]

        # Sort descending to simulate Telegram's default order
        sorted_ids = sorted(message_ids, reverse=True)
        messages = [_make_message(mid) for mid in sorted_ids]

        async def fake_iter_messages(entity, limit, max_id, reverse):
            iter_kwargs.append({"max_id": max_id, "reverse": reverse, "limit": limit})
            for m in messages:
                yield m

        client_mgr.client.iter_messages = fake_iter_messages
        worker._get_or_create_backfill_state = AsyncMock(return_value=0)
        worker._mark_job_in_progress = AsyncMock()
        worker._mark_job_completed = AsyncMock()
        worker._update_backfill_state = AsyncMock()
        worker._write_message = AsyncMock()
        worker._enqueue_media = AsyncMock()
        worker._upsert_user = AsyncMock()
        worker._write_user_sighting = AsyncMock()

        job = _make_job(1, chat_id, account_id)
        await worker._process_job(job)

    asyncio.run(run())

    assert iter_kwargs, "iter_messages was never called"
    for kwargs in iter_kwargs:
        assert kwargs["reverse"] is False, (
            f"Expected reverse=False for descending order, got reverse={kwargs['reverse']}"
        )


# ---------------------------------------------------------------------------
# Property 12: Backfill Batch Size Limit
# Validates: Requirements 1.10
#
# The limit passed to iter_messages must not exceed COLLECTOR_BACKFILL_BATCH_SIZE.
# ---------------------------------------------------------------------------

@given(
    batch_size=st.integers(min_value=1, max_value=500),
    chat_id=st.integers(min_value=1, max_value=10**9),
)
@h_settings(max_examples=25)
def test_property_12_backfill_batch_size_limit(
    batch_size: int, chat_id: int
) -> None:
    """**Validates: Requirements 1.10**

    The limit passed to iter_messages must not exceed COLLECTOR_BACKFILL_BATCH_SIZE.
    """
    captured_limits: list[int] = []

    async def run():
        account_id = 1
        worker = _make_worker()
        client_mgr = _make_client_manager(account_id)
        worker.clients = [client_mgr]

        async def fake_iter_messages(entity, limit, max_id, reverse):
            captured_limits.append(limit)
            return
            yield

        client_mgr.client.iter_messages = fake_iter_messages
        worker._get_or_create_backfill_state = AsyncMock(return_value=0)
        worker._mark_job_in_progress = AsyncMock()
        worker._mark_job_completed = AsyncMock()
        worker._update_backfill_state = AsyncMock()

        job = _make_job(1, chat_id, account_id)

        # Patch settings.COLLECTOR_BACKFILL_BATCH_SIZE
        with patch("services.collector.backfill_worker.settings") as mock_settings:
            mock_settings.COLLECTOR_BACKFILL_BATCH_SIZE = batch_size
            mock_settings.COLLECTOR_BACKFILL_CHAT_DELAY = 0
            await worker._process_job(job)

    asyncio.run(run())

    assert captured_limits, "iter_messages was never called"
    for limit in captured_limits:
        assert limit <= batch_size, (
            f"limit={limit} exceeds COLLECTOR_BACKFILL_BATCH_SIZE={batch_size}"
        )


# ---------------------------------------------------------------------------
# Property 13: Backfill Progress Update
# Validates: Requirements 1.7, 2.2
#
# After processing a batch, last_processed_message_id is set to min(batch_ids).
# ---------------------------------------------------------------------------

@given(
    message_ids=st.lists(
        st.integers(min_value=1, max_value=10**9),
        min_size=1,
        max_size=50,
        unique=True,
    ),
    chat_id=st.integers(min_value=1, max_value=10**9),
    account_id=st.integers(min_value=1, max_value=1000),
)
@h_settings(max_examples=25)
def test_property_13_backfill_progress_update(
    message_ids: list[int], chat_id: int, account_id: int
) -> None:
    """**Validates: Requirements 1.7, 2.2**

    After processing a batch, last_processed_message_id equals min(message_ids).
    """
    state_updates: list[dict] = []

    async def run():
        worker = _make_worker()
        client_mgr = _make_client_manager(account_id)
        worker.clients = [client_mgr]

        messages = [_make_message(mid) for mid in message_ids]

        async def fake_iter_messages(entity, limit, max_id, reverse):
            for m in messages:
                yield m

        client_mgr.client.iter_messages = fake_iter_messages
        worker._get_or_create_backfill_state = AsyncMock(return_value=0)
        worker._mark_job_in_progress = AsyncMock()
        worker._mark_job_completed = AsyncMock()
        worker._write_message = AsyncMock()
        worker._enqueue_media = AsyncMock()
        worker._upsert_user = AsyncMock()
        worker._write_user_sighting = AsyncMock()

        async def capture_state_update(c, a, **fields):
            state_updates.append(fields)

        worker._update_backfill_state = capture_state_update

        job = _make_job(1, chat_id, account_id)
        await worker._process_job(job)

    asyncio.run(run())

    # Find updates that set last_processed_message_id
    progress_updates = [
        u["last_processed_message_id"]
        for u in state_updates
        if "last_processed_message_id" in u
    ]

    assert progress_updates, "Expected at least one last_processed_message_id update"
    # The first progress update should be min(message_ids)
    expected_min = min(message_ids)
    assert progress_updates[0] == expected_min, (
        f"Expected last_processed_message_id={expected_min}, "
        f"got {progress_updates[0]}"
    )


# ---------------------------------------------------------------------------
# Property 16: Error Isolation
# Validates: Requirements 9.1
#
# When one job fails, other jobs continue processing without interruption.
# ---------------------------------------------------------------------------

@given(
    n_jobs=st.integers(min_value=2, max_value=6),
    failing_job_index=st.integers(min_value=0, max_value=5),
)
@h_settings(max_examples=25)
def test_property_16_error_isolation(
    n_jobs: int, failing_job_index: int
) -> None:
    """**Validates: Requirements 9.1**

    One job failure does not prevent other jobs from being processed.
    """
    assume(failing_job_index < n_jobs)

    async def run() -> tuple[list[int], list[int]]:
        _processed: list[int] = []
        _failed: list[int] = []

        worker = _make_worker()
        jobs = [_make_job(i + 1, chat_id=1000 + i, account_id=i + 1) for i in range(n_jobs)]

        clients = []
        for i in range(n_jobs):
            mgr = _make_client_manager(i + 1)
            if i == failing_job_index:
                async def fail_iter(entity, limit, max_id, reverse, _i=i):
                    raise RuntimeError(f"simulated failure for job {_i}")
                    yield
                mgr.client.iter_messages = fail_iter
            else:
                async def ok_iter(entity, limit, max_id, reverse, _i=i):
                    return
                    yield
                mgr.client.iter_messages = ok_iter
            clients.append(mgr)

        worker.clients = clients
        worker._get_or_create_backfill_state = AsyncMock(return_value=0)
        worker._mark_job_in_progress = AsyncMock()
        worker._update_backfill_state = AsyncMock()
        worker._write_message = AsyncMock()

        async def capture_completed(job):
            _processed.append(job["id"])

        worker._mark_job_completed = capture_completed

        async def capture_failed(job, error):
            _failed.append(job["id"])

        worker._mark_job_failed = capture_failed

        # Process all jobs — _process_job handles errors internally (no re-raise)
        for job in jobs:
            await worker._process_job(job)

        return _processed, _failed

    processed_jobs, failed_jobs = asyncio.run(run())

    total = len(processed_jobs) + len(failed_jobs)
    assert total == n_jobs, (
        f"Expected {n_jobs} jobs processed/failed, got {total}"
    )
    assert (failing_job_index + 1) in failed_jobs, (
        f"Job {failing_job_index + 1} should have failed"
    )
    other_job_ids = [i + 1 for i in range(n_jobs) if i != failing_job_index]
    for jid in other_job_ids:
        assert jid in processed_jobs, (
            f"Job {jid} should have been processed despite job "
            f"{failing_job_index + 1} failing"
        )

