"""Tests for src/core/spider_discover.py — generalised follower/following spider.

These tests use a stub fetcher (no network) and skip the DB-backed tests
unless a live postgres pool is available via SPIDER_TEST_DSN env var.
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator

import pytest
import pytest_asyncio

from src.core.spider_discover import (
    Edge,
    EdgeFetcher,
    EdgeType,
    SpiderDiscover,
    _InMemoryVisited,
)


# ── stub fetcher ─────────────────────────────────────────────────────────────


class StubFetcher:
    """Returns a fixed adjacency map. Edges only enumerated for FOLLOWING."""

    supported_edge_types = (EdgeType.FOLLOWING,)

    def __init__(self, graph: dict[str, list[str]]) -> None:
        self.graph = graph
        self.fetch_calls: list[tuple[str, EdgeType]] = []

    async def fetch_edges(
        self, node_id: str, edge_type: EdgeType
    ) -> AsyncIterator[Edge]:
        self.fetch_calls.append((node_id, edge_type))
        for neighbour in self.graph.get(node_id, []):
            yield Edge(source=node_id, target=neighbour, edge_type=edge_type)


# ── EdgeFetcher protocol check ───────────────────────────────────────────────


def test_stub_fetcher_satisfies_protocol():
    f = StubFetcher({})
    assert isinstance(f, EdgeFetcher)


def test_bad_fetcher_rejected():
    class NotAFetcher:
        pass

    # Constructor checks Protocol conformance — should raise TypeError
    with pytest.raises(TypeError):
        SpiderDiscover(
            platform="x", fetcher=NotAFetcher(), pool=None, max_hops=1
        )


# ── _InMemoryVisited ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_in_memory_visited_add_if_absent():
    v = _InMemoryVisited()
    assert await v.add_if_absent("a") is True
    assert await v.add_if_absent("a") is False
    assert await v.contains("a") is True
    assert await v.size() == 1


@pytest.mark.asyncio
async def test_in_memory_visited_concurrent():
    """Concurrent add_if_absent on same node — exactly one True."""
    v = _InMemoryVisited()
    results = await asyncio.gather(
        *[v.add_if_absent("x") for _ in range(20)]
    )
    assert results.count(True) == 1
    assert results.count(False) == 19


# ── parameter validation ─────────────────────────────────────────────────────


def test_negative_max_hops_rejected():
    with pytest.raises(ValueError, match="max_hops"):
        SpiderDiscover(
            platform="x", fetcher=StubFetcher({}), pool=None, max_hops=-1
        )


def test_zero_concurrency_rejected():
    with pytest.raises(ValueError, match="concurrency"):
        SpiderDiscover(
            platform="x",
            fetcher=StubFetcher({}),
            pool=None,
            max_hops=1,
            concurrency=0,
        )


# ── BFS behaviour against live DB ────────────────────────────────────────────
#
# These tests need a real Postgres connection because the spider's queue is
# a Postgres table. Skip cleanly if asyncpg or DB are unavailable.

DSN = os.environ.get("SPIDER_TEST_DSN")
RUN_DB_TESTS = bool(DSN)
skip_no_db = pytest.mark.skipif(
    not RUN_DB_TESTS, reason="SPIDER_TEST_DSN not set"
)


@pytest_asyncio.fixture
async def db_pool():
    if not RUN_DB_TESTS:
        pytest.skip("SPIDER_TEST_DSN not set")
    import asyncpg

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    # Cleanup leftovers from prior runs
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM spider_queue WHERE platform LIKE 'test_%'"
        )
    yield pool
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM spider_queue WHERE platform LIKE 'test_%'"
        )
    await pool.close()


@skip_no_db
@pytest.mark.asyncio
async def test_bfs_traversal_full_graph(db_pool):
    """Seed=A, max_hops=2, expect visited = {A,B,C,D,E,F}."""
    graph = {
        "A": ["B", "C"],
        "B": ["D"],
        "C": ["E", "F"],
        "D": [],
        "E": [],
        "F": [],
    }
    fetcher = StubFetcher(graph)
    spider = SpiderDiscover(
        platform="test_bfs_full",
        fetcher=fetcher,
        pool=db_pool,
        max_hops=2,
        concurrency=2,
    )
    await spider.seed("A")
    await spider.run()

    visited = {n for n, _ in fetcher.fetch_calls}
    # max_hops=2 — hop 0 (A), hop 1 (B,C), hop 2 (D,E,F) ALL fetched.
    # Children at hop 3 would be recorded as edges but NOT enqueued.
    assert visited == {"A", "B", "C", "D", "E", "F"}


@skip_no_db
@pytest.mark.asyncio
async def test_max_hops_zero_only_seed(db_pool):
    """max_hops=0 means seed only, no neighbours fetched."""
    graph = {"A": ["B", "C"]}
    fetcher = StubFetcher(graph)
    spider = SpiderDiscover(
        platform="test_zero_hop",
        fetcher=fetcher,
        pool=db_pool,
        max_hops=0,
        concurrency=1,
    )
    await spider.seed("A")
    await spider.run()
    # max_hops=0 — A is fetched (hop 0), B/C are recorded as edges but not enqueued
    visited = {n for n, _ in fetcher.fetch_calls}
    assert visited == {"A"}


@skip_no_db
@pytest.mark.asyncio
async def test_resume_from_in_progress(db_pool):
    """A row left in ``in_progress`` from a crashed run is reset to pending."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO spider_queue (platform, node_id, hop_distance,
               priority, status) VALUES ($1, $2, 0, 5, 'in_progress')""",
            "test_resume",
            "stuck_node",
        )
    fetcher = StubFetcher({"stuck_node": []})
    spider = SpiderDiscover(
        platform="test_resume",
        fetcher=fetcher,
        pool=db_pool,
        max_hops=1,
    )
    n_reset = await spider.reset_stuck_in_progress()
    assert n_reset == 1


@skip_no_db
@pytest.mark.asyncio
async def test_seed_idempotent(db_pool):
    """Seeding the same node twice — second call returns False (no insert)."""
    fetcher = StubFetcher({"X": []})
    spider = SpiderDiscover(
        platform="test_seed_idem", fetcher=fetcher, pool=db_pool, max_hops=1
    )
    inserted_first = await spider.seed("X")
    inserted_second = await spider.seed("X")
    assert inserted_first is True
    assert inserted_second is False
