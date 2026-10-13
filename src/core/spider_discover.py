"""Generalised follower/following graph spider — Wave 0 cross-cutting module.

Drives BFS traversal of social/code-graph relationships across 6 platforms
(github, instagram, tiktok, strava, lemon8, +others). Each platform plugs in
its own ``EdgeFetcher`` implementing the read-only edge enumeration API; this
module owns the queue, hop-distance accounting, claim leases, and concurrency.

Design summary
--------------
* **Pluggable fetcher** — ``EdgeFetcher`` Protocol. Each platform's collector
  provides a concrete fetcher that knows how to enumerate followers/following/
  kudos/stars/forks/collaborators for a given node id. Fetchers are async
  iterators so they can stream paginated results without buffering whole
  follower lists in RAM.
* **Hop-distance BFS** — work is dequeued in (hop_distance ASC, priority ASC,
  enqueued_at ASC) order. ``max_hops`` (default 2) caps depth — children of
  a node at the boundary are recorded as edges but NOT enqueued.
* **Persistent queue** — backed by Postgres ``spider_queue`` (composite PK
  platform+node_id). Claims carry renewable leases; startup requeues only
  expired claims so an active worker is not displaced.
* **Node deduplication** — the Postgres key prevents duplicate queue rows.
  Optional in-memory or Redis completion sets are informational and never
  suppress pending work or retries.
* **Rate-aware** — caller passes any awaitable ``rate_waiter`` that's awaited
  before each fetch. Compatible with src/core/rate_limit.AdaptiveRateLimiter
  (``await rl.wait(key)``).
* **Concurrent fetcher pool** — configurable concurrency (default 4) backed
  by ``asyncio.Semaphore``. Each in-flight fetch runs through a
  ``CircuitBreaker`` so a sustained-broken platform fetcher doesn't block
  the whole spider.
* **Edge type abstraction** — fetchers declare which edge types they support
  via ``EdgeFetcher.supported_edge_types``. The spider iterates only those.

DROP rules (per Wave 0 spec):
* This module is READ-ONLY. No write/follow operations. No DM crawling.
* It does NOT handle Telegram common-chat-membership — that's a separate
  ``telegram_account_chat_membership`` table (Batch 5 work).

Wiring (downstream Wave 2):
* TODO[github]:  src/collectors/github.py — wrap existing star/fork/contributor
                 enumeration in an EdgeFetcher and remove the bespoke
                 github_spider_queue logic.
* TODO[instagram]: src/collectors/instagram.py — followers/following via
                   Instaloader. Remove instagram_spider_queue per-row UPSERTs.
* TODO[tiktok]:  src/collectors/tiktok.py — followers/following via web API
                 (cookie-auth flow already exists).
* TODO[strava]:  src/collectors/strava.py — kudos/followers/following.
* TODO[lemon8]:  src/collectors/lemon8.py — followers/following.
* TODO[generic]: any platform with a public follow-graph.

Usage
-----

    from src.core.spider_discover import (
        SpiderDiscover, EdgeFetcher, Edge, EdgeType,
    )

    class GithubFetcher:
        supported_edge_types = (EdgeType.FOLLOWING, EdgeType.STAR)
        async def fetch_edges(self, node_id, edge_type):
            async for login in github_api.iter_following(node_id):
                yield Edge(source=node_id, target=login, edge_type=edge_type)

    spider = SpiderDiscover(
        platform="github", fetcher=GithubFetcher(), pool=pg_pool,
        max_hops=2, concurrency=4, rate_waiter=lambda: rl.wait("github"),
    )
    await spider.seed("torvalds")
    await spider.run()
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable

from src.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from src.core.proximity import refresh_account_proximity_cache

logger = logging.getLogger(__name__)


# ── public types ─────────────────────────────────────────────────────────────


class EdgeType(str, Enum):
    """Edge types a fetcher may enumerate. ``str`` mixin makes the value
    safe to drop directly into VARCHAR columns / log fields without
    a ``.value`` accessor."""
    FOLLOWER = "follower"
    FOLLOWING = "following"
    KUDOS = "kudos"
    STAR = "star"
    FORK = "fork"
    COLLABORATOR = "collaborator"
    CONTRIBUTOR = "contributor"
    SUBSCRIBER = "subscriber"  # YouTube etc.


@dataclass(frozen=True)
class Edge:
    """A directed edge produced by a fetcher.

    ``source`` is the node we asked about; ``target`` is the discovered
    neighbour. ``edge_type`` echoes the edge type passed to the fetcher;
    ``metadata`` is opaque and may be persisted or ignored by the caller.
    """
    source: str
    target: str
    edge_type: EdgeType
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class EdgeFetcher(Protocol):
    """Protocol every per-platform fetcher must implement.

    ``supported_edge_types`` is read once during ``SpiderDiscover.run`` to
    determine which edge types to enumerate per node. Implementations
    SHOULD raise ``NotImplementedError`` from ``fetch_edges`` for unsupported
    types — but the spider will not call them if they're absent from
    ``supported_edge_types``.
    """

    supported_edge_types: tuple[EdgeType, ...]

    def fetch_edges(
        self, node_id: str, edge_type: EdgeType
    ) -> AsyncIterator[Edge]:
        """Yield Edge objects rooted at ``node_id`` for ``edge_type``.

        Implementations are expected to be async generators (``async def``
        with ``yield``). Failures should raise — the spider wraps each
        invocation in a circuit breaker and per-call retry.
        """
        ...


# ── visited-set adaptors ─────────────────────────────────────────────────────


class _InMemoryVisited:
    """Default process-local record of successfully completed nodes."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = asyncio.Lock()

    async def add_if_absent(self, node_id: str) -> bool:
        async with self._lock:
            if node_id in self._seen:
                return False
            self._seen.add(node_id)
            return True

    async def contains(self, node_id: str) -> bool:
        async with self._lock:
            return node_id in self._seen

    async def discard(self, node_id: str) -> None:
        async with self._lock:
            self._seen.discard(node_id)

    async def size(self) -> int:
        async with self._lock:
            return len(self._seen)


class RedisVisited:
    """Redis-backed completion set. Queue state remains authoritative; this set
    is informational and never suppresses pending or retried queue work.

    Key shape: ``spider:visited:<platform>:<run_id>``. TTL configurable
    (default 7 days). ``add_if_absent`` is implemented via SADD return value
    (1 = inserted/new, 0 = already present). Existing markers never suppress
    work; the database queue owns completion and retry state."""

    def __init__(
        self,
        redis_client,
        platform: str,
        run_id: str,
        ttl_seconds: int = 7 * 24 * 3600,
    ) -> None:
        self._r = redis_client
        self._key = f"spider:visited:{platform}:{run_id}"
        self._ttl = ttl_seconds
        self._touched = False

    async def add_if_absent(self, node_id: str) -> bool:
        added = await self._r.sadd(self._key, node_id)
        if not self._touched:
            await self._r.expire(self._key, self._ttl)
            self._touched = True
        return bool(added)

    async def contains(self, node_id: str) -> bool:
        return bool(await self._r.sismember(self._key, node_id))

    async def discard(self, node_id: str) -> None:
        await self._r.srem(self._key, node_id)

    async def size(self) -> int:
        return int(await self._r.scard(self._key))


# ── stats ────────────────────────────────────────────────────────────────────


@dataclass
class SpiderStats:
    nodes_visited: int = 0
    nodes_failed: int = 0
    edges_yielded: int = 0
    edges_enqueued: int = 0
    fetch_errors: int = 0
    circuit_open_skips: int = 0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def snapshot(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["elapsed"] = (
            (self.finished_at or time.monotonic()) - self.started_at
            if self.started_at
            else None
        )
        return d


# ── the main engine ──────────────────────────────────────────────────────────


RateWaiter = Callable[[], Awaitable[Any]]
EdgeSink = Callable[[Edge, int], Awaitable[None]]
"""Optional callback invoked for every edge produced. ``hop_distance`` is the
BFS depth of the SOURCE node; the target is at depth+1. Use this to persist
edges to your platform's relationship table."""


class SpiderDiscover:
    """BFS spider engine. One instance per (platform, run).

    Parameters
    ----------
    platform : str
        Identifier persisted into the queue (e.g. "github", "instagram").
    fetcher : EdgeFetcher
        Plugged in concrete implementation.
    pool : asyncpg.Pool
        Active DB pool.
    max_hops : int
        Maximum BFS depth from any seed (default 2). Hop 0 = seed node.
    concurrency : int
        Max in-flight ``fetch_edges`` calls (default 4).
    rate_waiter : RateWaiter | None
        Awaitable invoked before every fetch_edges call. Pass
        ``lambda: rate_limiter.wait("github")`` to rate-limit.
    edge_sink : EdgeSink | None
        Optional callback for every edge produced.
    visited : visited-set adaptor | None
        Optional completion record. Queue state remains authoritative.
    breaker : CircuitBreaker | None
        Wraps each fetch call. Default: per-spider new breaker with
        threshold=10, recovery=60s.
    """

    def __init__(
        self,
        platform: str,
        fetcher: EdgeFetcher,
        pool,
        *,
        max_hops: int = 2,
        concurrency: int = 4,
        rate_waiter: Optional[RateWaiter] = None,
        edge_sink: Optional[EdgeSink] = None,
        visited=None,
        breaker: Optional[CircuitBreaker] = None,
        max_attempts: int = 5,
        retry_base_seconds: float = 30.0,
        retry_max_seconds: float = 1800.0,
        lease_seconds: int = 300,
        heartbeat_seconds: int = 60,
    ) -> None:
        if max_hops < 0:
            raise ValueError(f"max_hops must be >=0, got {max_hops}")
        if concurrency < 1:
            raise ValueError(f"concurrency must be >=1, got {concurrency}")
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >=1, got {max_attempts}")
        if retry_base_seconds < 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("retry delays must satisfy 0 <= base <= max")
        if lease_seconds < 3:
            raise ValueError(f"lease_seconds must be >=3, got {lease_seconds}")
        if heartbeat_seconds < 1 or heartbeat_seconds >= lease_seconds:
            raise ValueError(
                "heartbeat_seconds must be >=1 and less than lease_seconds"
            )
        if not isinstance(fetcher, EdgeFetcher):
            # Protocol check — duck-typed; raise if it lacks the required attrs.
            raise TypeError(
                "fetcher does not implement EdgeFetcher protocol "
                "(missing supported_edge_types or fetch_edges)"
            )

        self.platform = platform
        self.fetcher = fetcher
        self.pool = pool
        self.max_hops = max_hops
        self.concurrency = concurrency
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._rate_waiter = rate_waiter
        self._edge_sink = edge_sink
        self._visited = visited or _InMemoryVisited()
        self._breaker = breaker or CircuitBreaker(
            name=f"spider:{platform}",
            failure_threshold=10,
            recovery_timeout=60.0,
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._stop = asyncio.Event()
        self.stats = SpiderStats()

    # -- queue ops ----------------------------------------------------------

    async def reset_stuck_in_progress(self) -> int:
        """Requeue only expired claims; active work owned by another worker stays claimed.

        Called automatically by ``run()``. Returns the number of rows reset.
        Safe to call repeatedly.
        """
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE spider_queue
                   SET status = CASE WHEN attempts >= max_attempts
                                     THEN 'failed' ELSE 'pending' END,
                       terminal_reason = CASE WHEN attempts >= max_attempts
                                              THEN 'max_attempts_exceeded'
                                              ELSE terminal_reason END,
                       claim_token = NULL,
                       claimed_at = NULL,
                       lease_expires_at = NULL
                 WHERE platform = $1
                   AND status = 'in_progress'
                   AND (lease_expires_at IS NULL OR lease_expires_at <= NOW())
                """,
                self.platform,
            )
        # asyncpg returns "UPDATE N"
        try:
            n = int(result.split()[-1])
        except (ValueError, IndexError):
            n = 0
        if n:
            logger.info(
                "spider[%s] reset %d stuck in_progress rows", self.platform, n
            )
        return n

    async def seed(
        self,
        node_id: str,
        *,
        priority: int = 5,
        edge_type: Optional[EdgeType] = None,
        discovery_type: Optional[str] = None,
        source_table: Optional[str] = None,
        source_record_id: Optional[str] = None,
        source_metadata: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        max_attempts: Optional[int] = None,
    ) -> bool:
        """Seed the queue at hop 0. Returns True if newly inserted, False
        if already present (already-seeded or already-completed runs are
        not disturbed)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO spider_queue (
                    platform, node_id, hop_distance, priority, status,
                    parent_node_id, edge_type, discovery_type, source_table,
                    source_record_id, source_metadata, idempotency_key,
                    max_attempts, enqueued_at
                ) VALUES ($1, $2, 0, $3, 'pending', NULL, $4, $5, $6,
                          $7, $8::jsonb, $9, $10, NOW())
                ON CONFLICT DO NOTHING
                RETURNING node_id
                """,
                self.platform, node_id, priority,
                edge_type.value if edge_type else None,
                discovery_type,
                source_table,
                source_record_id,
                json.dumps(source_metadata or {}, default=str),
                idempotency_key or f"{self.platform}:{node_id}",
                self._resolve_max_attempts(max_attempts),
            )
        return row is not None

    async def _enqueue_child(
        self,
        conn,
        target: str,
        hop_distance: int,
        priority: int,
        parent_node_id: str,
        edge_type: EdgeType,
    ) -> bool:
        row = await conn.fetchrow(
            """
            INSERT INTO spider_queue (
                platform, node_id, hop_distance, priority, status,
                parent_node_id, edge_type, discovery_type, source_metadata,
                idempotency_key, max_attempts, enqueued_at
            ) VALUES ($1, $2, $3, $4, 'pending', $5, $6, 'graph_edge',
                      $7::jsonb, $8, $9, NOW())
            ON CONFLICT DO NOTHING
            RETURNING node_id
            """,
            self.platform, target, hop_distance, priority,
            parent_node_id, edge_type.value,
            json.dumps({}, default=str), f"{self.platform}:{target}",
            self.max_attempts,
        )
        return row is not None

    async def _claim_next(self) -> Optional[dict[str, Any]]:
        """Atomically claim the next pending row using SELECT ... FOR UPDATE
        SKIP LOCKED to prevent two workers from claiming the same node."""
        await refresh_account_proximity_cache(self.pool)
        claim_token = str(uuid.uuid4())
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT sq.platform, sq.node_id, sq.hop_distance, sq.priority,
                           sq.parent_node_id, sq.edge_type, sq.attempts,
                           sq.max_attempts, sq.discovery_type, sq.source_table,
                           sq.source_record_id, sq.source_metadata,
                           sq.idempotency_key
                      FROM spider_queue sq
                      LEFT JOIN LATERAL (
                        SELECT MIN(ap.tier) AS proximity_tier
                        FROM account_proximity_cache ap
                        WHERE ap.platform = sq.platform
                          AND ap.account_id = lower(sq.node_id)
                      ) prox ON TRUE
                     WHERE sq.platform = $1
                       AND sq.status = 'pending'
                       AND (sq.next_attempt_at IS NULL OR sq.next_attempt_at <= NOW())
                     ORDER BY
                        CASE
                            WHEN prox.proximity_tier IN (1, 2) THEN 2
                            WHEN prox.proximity_tier = 3 THEN 1
                            ELSE 0
                        END DESC,
                        sq.hop_distance ASC, sq.priority ASC, sq.enqueued_at ASC
                     LIMIT 1
                    FOR UPDATE OF sq SKIP LOCKED
                    """,
                    self.platform,
                )
                if row is None:
                    return None
                claimed = await conn.fetchrow(
                    """
                    UPDATE spider_queue
                       SET status = 'in_progress',
                           last_attempted_at = NOW(),
                           attempts = attempts + 1,
                           claimed_at = NOW(),
                           lease_expires_at = NOW() + make_interval(secs => $4::int),
                           claim_token = $3,
                           next_attempt_at = NULL
                     WHERE platform = $1 AND node_id = $2
                    RETURNING attempts, claim_token
                    """,
                    self.platform, row["node_id"], claim_token,
                    self.lease_seconds,
                )
                claim = dict(row)
                if claimed is None:
                    return None
                claim.update(dict(claimed))
                return claim

    async def _mark_completed(self, node_id: str, claim_token: uuid.UUID) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE spider_queue
                   SET status = 'completed', completed_at = NOW(), error = NULL,
                       terminal_reason = NULL, next_attempt_at = NULL,
                       claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL
                 WHERE platform = $1 AND node_id = $2
                   AND status = 'in_progress' AND claim_token = $3
                """,
                self.platform, node_id, claim_token,
            )
        return result.endswith(" 1")

    async def _mark_failed(
        self, node_id: str, err: str, claim: dict[str, Any]
    ) -> str | None:
        attempts = int(claim["attempts"])
        max_attempts = int(claim["max_attempts"])
        terminal = attempts >= max_attempts
        delay = self._retry_delay_seconds(attempts)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE spider_queue
                   SET status = $4::varchar(20),
                       error = $3,
                       terminal_reason = $5,
                       next_attempt_at = CASE
                           WHEN $4::varchar(20) = 'pending'::varchar(20)
                           THEN NOW() + make_interval(secs => $6::double precision)
                           ELSE NULL END,
                       claim_token = NULL,
                       claimed_at = NULL,
                       lease_expires_at = NULL
                 WHERE platform = $1 AND node_id = $2
                   AND status = 'in_progress' AND claim_token = $7
                RETURNING status
                """,
                self.platform, node_id, err[:1000],
                "failed" if terminal else "pending",
                "max_attempts_exceeded" if terminal else None,
                delay,
                claim["claim_token"],
            )
        return row["status"] if row else None

    def _retry_delay_seconds(self, attempt: int) -> float:
        """Exponential retry delay with a configured cap."""
        delay = self.retry_base_seconds
        for _ in range(max(0, attempt - 1)):
            delay = min(delay * 2, self.retry_max_seconds)
            if delay >= self.retry_max_seconds:
                break
        return delay

    def _resolve_max_attempts(self, max_attempts: Optional[int]) -> int:
        value = self.max_attempts if max_attempts is None else max_attempts
        if value < 1:
            raise ValueError(f"max_attempts must be >=1, got {value}")
        return value

    async def _renew_claim(self, node_id: str, claim_token: uuid.UUID) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE spider_queue
                   SET lease_expires_at = NOW() + make_interval(secs => $4::int)
                 WHERE platform = $1 AND node_id = $2
                   AND status = 'in_progress' AND claim_token = $3
                """,
                self.platform, node_id, claim_token, self.lease_seconds,
            )
        return result.endswith(" 1")

    async def _heartbeat_claim(
        self,
        node_id: str,
        claim_token: uuid.UUID,
        worker_task: asyncio.Task[None],
    ) -> None:
        interval = min(self.heartbeat_seconds, self.lease_seconds / 3)
        lease_deadline = time.monotonic() + self.lease_seconds
        retry_interval = min(2.0, interval)
        safety_margin = min(interval, max(1.0, self.lease_seconds / 3))
        retry_cutoff = lease_deadline - safety_margin
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._renew_claim(node_id, claim_token)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "spider[%s] lease renewal failed for node=%s",
                    self.platform, node_id,
                )
                while time.monotonic() < retry_cutoff:
                    await asyncio.sleep(
                        min(retry_interval, retry_cutoff - time.monotonic())
                    )
                    try:
                        renewed = await self._renew_claim(node_id, claim_token)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.debug(
                            "spider[%s] lease renewal retry failed for node=%s",
                            self.platform, node_id, exc_info=True,
                        )
                        retry_interval = min(retry_interval * 2, 10.0, interval)
                        continue
                    break
                else:
                    logger.error(
                        "spider[%s] could not renew lease for node=%s before expiry; "
                        "cancelling worker",
                        self.platform, node_id,
                    )
                    worker_task.cancel()
                    return

            if not renewed:
                logger.warning(
                    "spider[%s] lost claim lease for node=%s",
                    self.platform, node_id,
                )
                worker_task.cancel()
                return
            lease_deadline = time.monotonic() + self.lease_seconds
            retry_cutoff = lease_deadline - safety_margin
            retry_interval = min(2.0, interval)

    # -- worker -------------------------------------------------------------

    async def _process_node(self, claim: dict[str, Any]) -> None:
        node_id = claim["node_id"]
        hop = claim["hop_distance"]
        claim_token = claim["claim_token"]
        worker_task = asyncio.current_task()
        if worker_task is None:
            raise RuntimeError("spider worker must run inside an asyncio task")
        heartbeat = asyncio.create_task(
            self._heartbeat_claim(node_id, claim_token, worker_task),
            name=f"spider-lease-{self.platform}-{node_id}",
        )

        try:
            self.stats.nodes_visited += 1

            for et in self.fetcher.supported_edge_types:
                async for edge in self._iter_edges_with_breaker(node_id, et):
                    self.stats.edges_yielded += 1
                    if self._edge_sink is not None:
                        try:
                            await self._edge_sink(edge, hop)
                        except Exception:
                            logger.exception(
                                "spider[%s] edge_sink raised for %s",
                                self.platform, edge.target,
                            )

                    # Enqueue child only if within max_hops (children at
                    # depth hop+1; if hop+1 > max_hops, record edge but
                    # don't expand further).
                    if hop + 1 <= self.max_hops:
                        async with self.pool.acquire() as conn:
                            inserted = await self._enqueue_child(
                                conn,
                                target=edge.target,
                                hop_distance=hop + 1,
                                priority=claim["priority"],
                                parent_node_id=node_id,
                                edge_type=et,
                            )
                        if inserted:
                            self.stats.edges_enqueued += 1

            completed = await self._mark_completed(node_id, claim_token)
            if completed:
                try:
                    await self._visited.add_if_absent(node_id)
                except Exception:
                    logger.exception(
                        "spider[%s] could not record completed node=%s",
                        self.platform, node_id,
                    )

        except Exception as exc:
            self.stats.nodes_failed += 1
            logger.exception(
                "spider[%s] node=%s failed: %s",
                self.platform, node_id, exc,
            )
            result = await self._mark_failed(node_id, repr(exc), claim)
            if result:
                logger.info(
                    "spider[%s] node=%s failure recorded as %s (attempt %s/%s)",
                    self.platform, node_id, result,
                    claim["attempts"], claim["max_attempts"],
                )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _iter_edges_with_breaker(
        self, node_id: str, edge_type: EdgeType
    ) -> AsyncIterator[Edge]:
        """Run fetcher.fetch_edges through the circuit breaker + rate waiter.

        We can't naively wrap an async generator in ``breaker.call`` (that
        expects a single awaitable return). So we collect the iterator's
        ``__anext__`` calls individually and treat each yield as one
        protected operation."""
        if self._rate_waiter is not None:
            try:
                await self._rate_waiter()
            except Exception:
                logger.exception("spider[%s] rate_waiter raised", self.platform)

        # Protect just the construction + first __anext__ via the breaker.
        # Subsequent yields run unprotected — the cost of protecting every
        # yield (a tight inner loop) outweighs the benefit, and the breaker
        # already saw the upstream's health on the first call.
        try:
            iterator = self.fetcher.fetch_edges(node_id, edge_type)
        except CircuitOpenError:
            self.stats.circuit_open_skips += 1
            return
        except Exception:
            self.stats.fetch_errors += 1
            raise

        # Drain the iterator, surfacing yields. We probe the first item
        # through the breaker; subsequent items run free.
        first_done = False
        while True:
            try:
                if not first_done:
                    try:
                        edge = await self._breaker.call(iterator.__anext__)
                    except CircuitOpenError:
                        self.stats.circuit_open_skips += 1
                        return
                    first_done = True
                else:
                    edge = await iterator.__anext__()
            except StopAsyncIteration:
                return
            except Exception:
                self.stats.fetch_errors += 1
                raise
            yield edge

    # -- run loop -----------------------------------------------------------

    def request_stop(self) -> None:
        """Cooperative shutdown. In-flight tasks finish their current node."""
        self._stop.set()

    async def run(self) -> SpiderStats:
        """Drain the queue until empty (or ``request_stop`` is called).

        Resets stuck in_progress rows on entry. Returns the SpiderStats
        snapshot when complete."""
        self.stats = SpiderStats(started_at=time.monotonic())
        await self.reset_stuck_in_progress()

        in_flight: set[asyncio.Task[None]] = set()

        try:
            while not self._stop.is_set():
                # Reap finished tasks
                if in_flight:
                    done, in_flight = await asyncio.wait(
                        in_flight, timeout=0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in done:
                        if t.cancelled():
                            continue
                        exc = t.exception()
                        if exc:
                            logger.error(
                                "spider[%s] worker task crashed: %r",
                                self.platform, exc,
                            )

                if len(in_flight) >= self.concurrency:
                    # Wait for at least one to finish before claiming more.
                    done, in_flight = await asyncio.wait(
                        in_flight, return_when=asyncio.FIRST_COMPLETED,
                    )
                    continue

                claim = await self._claim_next()
                if claim is None:
                    if not in_flight:
                        break  # truly empty
                    # No work right now but tasks are running — they may
                    # enqueue more. Wait for one to finish.
                    done, in_flight = await asyncio.wait(
                        in_flight, return_when=asyncio.FIRST_COMPLETED,
                    )
                    continue

                task = asyncio.create_task(
                    self._process_node(claim),
                    name=f"spider-{self.platform}-{claim['node_id']}",
                )
                in_flight.add(task)

            # Drain remaining
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
        finally:
            self.stats.finished_at = time.monotonic()
            logger.info(
                "spider[%s] finished: %s",
                self.platform, self.stats.snapshot(),
            )
        return self.stats


__all__ = [
    "SpiderDiscover",
    "EdgeFetcher",
    "Edge",
    "EdgeType",
    "SpiderStats",
    "RedisVisited",
]
