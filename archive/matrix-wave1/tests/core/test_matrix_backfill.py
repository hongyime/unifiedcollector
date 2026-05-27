"""Tests for src/core/matrix_backfill.py — backfill driver.

Pure-unit. The Matrix client, writer, and state repo are all replaced
with AsyncMock stubs; no asyncpg, no nio, no network. Validates:

  * list_priority_rooms sort order (by last_activity_ts DESC, NULLs last)
  * backfill_room: paginates, stops on target_depth / max_pages /
    end-of-history / persistent error
  * backfill_room: progress is upserted per page; mark_done called only
    on natural completion
  * backfill_room: skips rooms already marked done
  * backfill_all: bounded by semaphore, aggregates counters, surfaces
    per-room errors without crashing the cycle
  * helpers (_events_from_response, _end_token, _earliest_event_ts)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.core import matrix_backfill as mb
from src.core.matrix_backfill import MatrixBackfillDriver


# ── stub event/response builders ──────────────────────────────────────


def _event(event_id: str, ts_ms: int, body: str = "hello") -> dict:
    return {
        "event_id": event_id,
        "type": "m.room.message",
        "sender": "@u:beeper.com",
        "origin_server_ts": ts_ms,
        "content": {"msgtype": "m.text", "body": body},
    }


class StubMessagesResp:
    def __init__(self, chunk: list[dict], end: Optional[str]) -> None:
        self.chunk = chunk
        self.end = end


class StubRoomSummary:
    def __init__(self, room_id: str, last_activity_ts: Optional[int],
                 display_name: str = "room") -> None:
        self.room_id = room_id
        self.last_activity_ts = last_activity_ts
        self.display_name = display_name


# ── fixtures ──────────────────────────────────────────────────────────


def _make_pool_stub() -> Any:
    pool = MagicMock()
    return pool


@pytest_asyncio.fixture
async def driver():
    """Driver with all collaborators mocked."""
    client = MagicMock()
    client.list_rooms = AsyncMock(return_value={})
    client.fetch_history = AsyncMock()

    writer = MagicMock()
    writer.pool = _make_pool_stub()
    writer.write_batch = AsyncMock(return_value=0)

    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.fetch_pending = AsyncMock(return_value=[])
    repo.upsert_progress = AsyncMock()
    repo.mark_done = AsyncMock()

    return MatrixBackfillDriver(client=client, writer=writer, repo=repo)


# ── helper unit tests ─────────────────────────────────────────────────


def test_events_from_response_dict_chunk():
    resp = StubMessagesResp(chunk=[_event("$a", 1), _event("$b", 2)], end="t1")
    out = mb._events_from_response(resp)
    assert len(out) == 2
    assert out[0]["event_id"] == "$a"


def test_events_from_response_with_source_attr():
    class E:
        def __init__(self, src):
            self.source = src
    resp = StubMessagesResp(chunk=[E(_event("$a", 1))], end="t1")
    out = mb._events_from_response(resp)
    assert out == [_event("$a", 1)]


def test_events_from_response_empty():
    assert mb._events_from_response(StubMessagesResp([], None)) == []
    assert mb._events_from_response(StubMessagesResp([], "x")) == []


def test_end_token_attr_and_dict():
    assert mb._end_token(StubMessagesResp([], "tok")) == "tok"
    assert mb._end_token({"end": "tok2"}) == "tok2"
    assert mb._end_token(StubMessagesResp([], None)) is None


def test_earliest_event_ts_picks_min():
    events = [_event("$a", 3000), _event("$b", 1000), _event("$c", 2000)]
    ts = mb._earliest_event_ts(events)
    assert ts == datetime.fromtimestamp(1.0, tz=timezone.utc)


def test_earliest_event_ts_none_for_empty():
    assert mb._earliest_event_ts([]) is None
    assert mb._earliest_event_ts([{"type": "x"}]) is None


# ── list_priority_rooms ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_priority_rooms_sorts_by_last_activity_desc(driver):
    driver.client.list_rooms = AsyncMock(return_value={
        "!a:b": StubRoomSummary("!a:b", 100, "old"),
        "!b:b": StubRoomSummary("!b:b", 300, "newest"),
        "!c:b": StubRoomSummary("!c:b", None, "unknown"),
        "!d:b": StubRoomSummary("!d:b", 200, "mid"),
    })
    out = await driver.list_priority_rooms()
    ids = [r[0] for r in out]
    assert ids[0] == "!b:b"  # newest first
    assert ids[1] == "!d:b"
    assert ids[2] == "!a:b"
    assert ids[-1] == "!c:b"  # NULL last


# ── backfill_room — happy path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_room_skips_when_already_done(driver):
    driver.repo.get = AsyncMock(return_value={"done": True, "last_token": None})
    out = await driver.backfill_room("!r:b")
    assert out["done"] is True
    assert out["events_fetched"] == 0
    assert out["pages_used"] == 0
    driver.client.fetch_history.assert_not_awaited()
    driver.repo.mark_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_room_paginates_until_natural_end(driver):
    # Two pages, then empty chunk + None end token => end of history.
    page1 = StubMessagesResp([_event("$1", 5000), _event("$2", 6000)], "t1")
    page2 = StubMessagesResp([_event("$3", 3000)], "t2")
    page3 = StubMessagesResp([], None)
    driver.client.fetch_history = AsyncMock(side_effect=[page1, page2, page3])
    driver.writer.write_batch = AsyncMock(side_effect=[2, 1, 0])

    out = await driver.backfill_room("!r:b", target_depth=1000, max_pages=10)

    assert out["events_fetched"] == 3
    assert out["pages_used"] == 3
    assert out["done"] is True
    assert out["error"] is None
    driver.repo.mark_done.assert_awaited_once_with("!r:b")
    assert driver.repo.upsert_progress.await_count == 3


@pytest.mark.asyncio
async def test_backfill_room_stops_on_max_pages(driver):
    # Always returns 1 event + a fresh token, so only max_pages stops us.
    def make():
        i = {"n": 0}
        def fn(**kwargs):
            i["n"] += 1
            return StubMessagesResp([_event(f"$e{i['n']}", 1000 + i["n"])], f"t{i['n']}")
        return fn
    fn = make()
    driver.client.fetch_history = AsyncMock(side_effect=lambda **kw: fn(**kw))
    driver.writer.write_batch = AsyncMock(return_value=1)

    out = await driver.backfill_room("!r:b", target_depth=1000, max_pages=3)

    assert out["pages_used"] == 3
    assert out["events_fetched"] == 3
    assert out["done"] is False  # max_pages, not natural end
    driver.repo.mark_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_room_stops_on_target_depth(driver):
    # Each page returns 100 inserts; target_depth=150 -> stop after page 2.
    # Use distinct tokens so the "unchanged token = end" guard doesn't fire.
    pages = [
        StubMessagesResp(
            [_event(f"$p{p}_e{i}", 1000 + p * 1000 + i) for i in range(100)],
            f"tok-{p}",
        )
        for p in range(5)
    ]
    driver.client.fetch_history = AsyncMock(side_effect=pages)
    driver.writer.write_batch = AsyncMock(return_value=100)

    out = await driver.backfill_room("!r:b", target_depth=150, max_pages=99)

    # First page: 100 events. After page 1, events_fetched=100 < 150,
    # loop continues; second page reaches 200 >= 150, loop exits.
    assert out["events_fetched"] >= 150
    assert out["pages_used"] == 2
    assert out["done"] is False


@pytest.mark.asyncio
async def test_backfill_room_handles_fetch_error(driver):
    driver.client.fetch_history = AsyncMock(side_effect=RuntimeError("boom"))

    out = await driver.backfill_room("!r:b", target_depth=1000, max_pages=5)

    assert out["events_fetched"] == 0
    assert out["pages_used"] == 0
    assert out["done"] is False
    assert "RuntimeError" in (out["error"] or "")
    driver.repo.mark_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_room_handles_write_error_persists_cursor(driver):
    page = StubMessagesResp([_event("$a", 1000)], "next-token")
    driver.client.fetch_history = AsyncMock(return_value=page)
    driver.writer.write_batch = AsyncMock(side_effect=ValueError("db down"))

    out = await driver.backfill_room("!r:b")

    assert out["error"] is not None
    assert out["done"] is False
    # Progress upsert MUST still be called so the cursor advances and the
    # error message is persisted for the next cycle.
    driver.repo.upsert_progress.assert_awaited()
    # mark_done must NOT be called on error.
    driver.repo.mark_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_room_resumes_from_persisted_token(driver):
    driver.repo.get = AsyncMock(return_value={
        "done": False,
        "last_token": "resume-here",
    })
    page = StubMessagesResp([], None)
    driver.client.fetch_history = AsyncMock(return_value=page)

    await driver.backfill_room("!r:b")

    call = driver.client.fetch_history.await_args
    assert call.kwargs["before_token"] == "resume-here"


@pytest.mark.asyncio
async def test_backfill_room_end_token_unchanged_means_done(driver):
    # Server echoing our cursor back = nothing earlier exists.
    driver.repo.get = AsyncMock(return_value={"done": False, "last_token": "T"})
    page = StubMessagesResp([_event("$a", 1)], "T")  # end == start
    driver.client.fetch_history = AsyncMock(return_value=page)
    driver.writer.write_batch = AsyncMock(return_value=1)

    out = await driver.backfill_room("!r:b")
    assert out["done"] is True
    driver.repo.mark_done.assert_awaited_once_with("!r:b")


# ── backfill_all ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_all_skips_done_rooms(driver):
    driver.client.list_rooms = AsyncMock(return_value={
        "!a:b": StubRoomSummary("!a:b", 100),
        "!b:b": StubRoomSummary("!b:b", 200),
    })
    # Both rooms already marked done.
    driver.repo.get = AsyncMock(return_value={"done": True})
    summary = await driver.backfill_all(concurrency=2, per_room_target=100)

    # Skipped rooms don't count as processed but still appear in per_room.
    assert summary["rooms_processed"] == 0
    assert summary["rooms_total"] == 2
    assert summary["events_fetched"] == 0
    assert summary["errors"] == 0
    driver.client.fetch_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_all_aggregates_results(driver):
    driver.client.list_rooms = AsyncMock(return_value={
        "!a:b": StubRoomSummary("!a:b", 200),
        "!b:b": StubRoomSummary("!b:b", 100),
    })
    driver.repo.get = AsyncMock(return_value=None)  # no prior state
    page = StubMessagesResp([_event("$a", 5)], None)
    driver.client.fetch_history = AsyncMock(return_value=page)
    driver.writer.write_batch = AsyncMock(return_value=1)

    summary = await driver.backfill_all(concurrency=2, per_room_target=50)
    assert summary["rooms_processed"] == 2
    assert summary["events_fetched"] == 2
    assert summary["errors"] == 0


@pytest.mark.asyncio
async def test_backfill_all_surfaces_errors_without_crash(driver):
    driver.client.list_rooms = AsyncMock(return_value={
        "!ok:b": StubRoomSummary("!ok:b", 200),
        "!bad:b": StubRoomSummary("!bad:b", 100),
    })
    driver.repo.get = AsyncMock(return_value=None)

    async def fh(**kw):
        if kw["room_id"] == "!bad:b":
            raise RuntimeError("rate limited")
        return StubMessagesResp([_event("$a", 1)], None)

    driver.client.fetch_history = AsyncMock(side_effect=fh)
    driver.writer.write_batch = AsyncMock(return_value=1)

    summary = await driver.backfill_all(concurrency=2, per_room_target=50)
    assert summary["rooms_processed"] == 2
    assert summary["errors"] == 1


@pytest.mark.asyncio
async def test_backfill_all_respects_room_limit(driver):
    driver.client.list_rooms = AsyncMock(return_value={
        f"!r{i}:b": StubRoomSummary(f"!r{i}:b", i) for i in range(10)
    })
    driver.repo.get = AsyncMock(return_value={"done": True})
    summary = await driver.backfill_all(concurrency=4, per_room_target=10,
                                         room_limit=3)
    assert summary["rooms_total"] == 3


@pytest.mark.asyncio
async def test_backfill_all_concurrency_bounded(driver):
    """At most `concurrency` rooms run in flight simultaneously."""
    driver.client.list_rooms = AsyncMock(return_value={
        f"!r{i}:b": StubRoomSummary(f"!r{i}:b", i) for i in range(8)
    })
    driver.repo.get = AsyncMock(return_value=None)

    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def fh(**kw):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        return StubMessagesResp([], None)

    driver.client.fetch_history = AsyncMock(side_effect=fh)
    driver.writer.write_batch = AsyncMock(return_value=0)

    await driver.backfill_all(concurrency=3, per_room_target=10)
    assert peak <= 3, f"concurrency exceeded: peak={peak}"


# ── construction guards ───────────────────────────────────────────────


def test_driver_requires_client():
    with pytest.raises(ValueError):
        MatrixBackfillDriver(client=None, writer=MagicMock())


def test_driver_requires_writer():
    with pytest.raises(ValueError):
        MatrixBackfillDriver(client=MagicMock(), writer=None)


def test_driver_builds_repo_from_writer_pool():
    writer = MagicMock()
    writer.pool = MagicMock()
    drv = MatrixBackfillDriver(client=MagicMock(), writer=writer)
    assert drv.repo is not None
