from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from src.collectors.bluesky import _parse_indexed_at
from src.collectors.bluesky.identity import (
    _canonical_handle,
    resolve_and_enqueue_bluesky_candidates,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.cache_rows: list[tuple[str, str | None]] = []
        self.queue_rows: list[tuple[str, str]] = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def executemany(self, _query: str, rows) -> None:
        self.cache_rows.extend(rows)

    async def fetch(self, _query: str, dids: list[str], enqueued_by: list[str]):
        self.queue_rows.extend(zip(dids, enqueued_by))
        return [{"username": did} for did in dids]


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConnection()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def test_canonical_handle_qualifies_cross_platform_usernames():
    assert _canonical_handle("Alice", cross_platform=True) == "alice.bsky.social"
    assert _canonical_handle("first.last", cross_platform=True) == "first.last.bsky.social"
    assert _canonical_handle("alice.example", cross_platform=False) == "alice.example"


def test_indexed_at_is_parsed_as_timezone_aware_datetime():
    parsed = _parse_indexed_at("2024-11-16T11:48:43.577Z")

    assert parsed is not None
    assert parsed.isoformat() == "2024-11-16T11:48:43.577000+00:00"
    assert _parse_indexed_at("not-a-timestamp") is None


async def test_only_resolved_dids_are_queued_and_misses_are_cached():
    requested_handles: list[str] = []
    did = "did:plc:abcdefghijklmnopqrstuvwx"

    def respond(request: httpx.Request) -> httpx.Response:
        handle = request.url.params["handle"]
        requested_handles.append(handle)
        if handle == "known.bsky.social":
            return httpx.Response(200, json={"did": did})
        return httpx.Response(404, json={"error": "HandleNotFound"})

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)

    pool = _FakePool()
    result = await resolve_and_enqueue_bluesky_candidates(
        pool,
        [
            {"username": "known", "enqueued_by": "cross_platform:x", "cross_platform": True},
            {"username": "missing", "enqueued_by": "cross_platform:x", "cross_platform": True},
        ],
        client_factory=client_factory,
        request_interval_seconds=0,
    )

    assert requested_handles == ["known.bsky.social", "missing.bsky.social"]
    assert pool.conn.cache_rows == [
        ("known.bsky.social", did),
        ("missing.bsky.social", None),
    ]
    assert pool.conn.queue_rows == [(did, "bluesky_handle:known.bsky.social:cross_platform:x")]
    assert result == {"resolved": 1, "unresolvable": 1, "queued": 1, "deferred": 0}


async def test_rate_limit_defers_rest_of_batch_without_queueing():
    requested_handles: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested_handles.append(request.url.params["handle"])
        return httpx.Response(429, headers={"Retry-After": "30"})

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)

    pool = _FakePool()
    result = await resolve_and_enqueue_bluesky_candidates(
        pool,
        [
            {"username": "one", "enqueued_by": "cross_platform:x", "cross_platform": True},
            {"username": "two", "enqueued_by": "cross_platform:x", "cross_platform": True},
        ],
        client_factory=client_factory,
        request_interval_seconds=0,
    )

    assert requested_handles == ["one.bsky.social"]
    assert pool.conn.cache_rows == []
    assert pool.conn.queue_rows == []
    assert result == {"resolved": 0, "unresolvable": 0, "queued": 0, "deferred": 2}
