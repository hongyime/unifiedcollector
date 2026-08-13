"""Domain-aware pacing shared by website and search crawlers.

The collector can run many workers overall, but public sites need pressure to be
spread across registrable-domain buckets. This module keeps that policy small:
round-robin URLs by domain, cap active domains, cap in-flight requests per
domain, add jitter, and write bounded telemetry for dashboard status.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterable
from urllib.parse import urlparse


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 1000) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 3600.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def normalize_host(host: str | None) -> str:
    """Normalize a network location without widening subdomain scope."""
    value = (host or "").strip().lower()
    if "@" in value:
        value = value.rsplit("@", 1)[-1]
    if value.startswith("[") and "]" in value:
        return value.split("]", 1)[0].strip("[]")
    if ":" in value:
        value = value.split(":", 1)[0]
    if value.startswith("www."):
        value = value[4:]
    return value.rstrip(".")


def host_from_url(url: str) -> str:
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        return normalize_host(parsed.netloc)
    except Exception:
        return ""


def registrable_domain_from_url(url: str) -> str:
    """Return the crawler bucket for a URL.

    This intentionally mirrors ``WebsiteCollector._registrable_domain``: only a
    leading ``www.`` is stripped. Deeper subdomains remain distinct to avoid
    widening crawl scope in existing tests and production behavior.
    """
    return host_from_url(url) or "unknown"


def round_robin_by_domain(urls: Iterable[str]) -> list[str]:
    """Interleave URLs so one domain cannot monopolize a worker batch."""
    groups: dict[str, deque[str]] = defaultdict(deque)
    order: list[str] = []
    for url in urls:
        domain = registrable_domain_from_url(url)
        if domain not in groups:
            order.append(domain)
        groups[domain].append(url)

    output: list[str] = []
    active = deque(order)
    while active:
        domain = active.popleft()
        bucket = groups[domain]
        output.append(bucket.popleft())
        if bucket:
            active.append(domain)
    return output


@dataclass
class DomainPacingSnapshot:
    source: str
    active_domains: int
    per_domain_inflight: dict[str, int]
    counters: dict[str, int]
    max_active_domains: int
    max_per_domain: int
    delay_seconds: float
    jitter_seconds: float
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "active_domains": self.active_domains,
            "per_domain_inflight": self.per_domain_inflight,
            "counters": self.counters,
            "max_active_domains": self.max_active_domains,
            "max_per_domain": self.max_per_domain,
            "delay_seconds": self.delay_seconds,
            "jitter_seconds": self.jitter_seconds,
            "checked_at": self.checked_at,
        }


class DomainPacer:
    """Async request pacer with active-domain and per-domain limits."""

    def __init__(
        self,
        source: str,
        *,
        env_prefix: str,
        max_active_domains: int = 4,
        max_per_domain: int = 2,
        delay_seconds: float = 1.5,
        jitter_seconds: float = 2.0,
    ) -> None:
        self.source = source
        self.max_active_domains = _env_int(
            f"{env_prefix}_MAX_ACTIVE_DOMAINS",
            max_active_domains,
            minimum=1,
            maximum=64,
        )
        self.max_per_domain = _env_int(
            f"{env_prefix}_MAX_REQUESTS_PER_DOMAIN",
            max_per_domain,
            minimum=1,
            maximum=16,
        )
        self.delay_seconds = _env_float(
            f"{env_prefix}_DOMAIN_DELAY_SECONDS",
            delay_seconds,
            minimum=0.0,
            maximum=300.0,
        )
        self.jitter_seconds = _env_float(
            f"{env_prefix}_DOMAIN_JITTER_SECONDS",
            jitter_seconds,
            minimum=0.0,
            maximum=300.0,
        )
        self._lock = asyncio.Lock()
        self._active_domains: set[str] = set()
        self._active_domain_sem = asyncio.Semaphore(self.max_active_domains)
        self._domain_sems: dict[str, asyncio.Semaphore] = {}
        self._domain_entry_locks: dict[str, asyncio.Lock] = {}
        self._domain_refs: Counter[str] = Counter()
        self._inflight: Counter[str] = Counter()
        self._counters: Counter[str] = Counter()

    def order(self, urls: Iterable[str]) -> list[str]:
        return round_robin_by_domain(urls)

    def snapshot(self) -> DomainPacingSnapshot:
        return DomainPacingSnapshot(
            source=self.source,
            active_domains=len(self._active_domains),
            per_domain_inflight={k: int(v) for k, v in self._inflight.items() if v > 0},
            counters=dict(self._counters),
            max_active_domains=self.max_active_domains,
            max_per_domain=self.max_per_domain,
            delay_seconds=self.delay_seconds,
            jitter_seconds=self.jitter_seconds,
        )

    def count(self, event_type: str, count: int = 1) -> None:
        self._counters[event_type] += count

    async def _domain_sem(self, domain: str) -> asyncio.Semaphore:
        async with self._lock:
            sem = self._domain_sems.get(domain)
            if sem is None:
                sem = asyncio.Semaphore(self.max_per_domain)
                self._domain_sems[domain] = sem
            return sem

    async def _domain_entry_lock(self, domain: str) -> asyncio.Lock:
        async with self._lock:
            lock = self._domain_entry_locks.get(domain)
            if lock is None:
                lock = asyncio.Lock()
                self._domain_entry_locks[domain] = lock
            return lock

    @contextlib.asynccontextmanager
    async def slot(self, url: str) -> AsyncIterator[str]:
        """Acquire a polite request slot for ``url`` and yield its domain key."""
        domain = registrable_domain_from_url(url)
        entry_lock = await self._domain_entry_lock(domain)
        async with entry_lock:
            async with self._lock:
                needs_active_domain = self._domain_refs[domain] <= 0
            if needs_active_domain:
                await self._active_domain_sem.acquire()
            async with self._lock:
                self._domain_refs[domain] += 1
                self._active_domains.add(domain)

        sem = await self._domain_sem(domain)
        await sem.acquire()
        async with self._lock:
            self._active_domains.add(domain)
            self._inflight[domain] += 1
            self._counters["requests_started"] += 1
        delay = self.delay_seconds + (random.uniform(0, self.jitter_seconds) if self.jitter_seconds else 0.0)
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            yield domain
        finally:
            sem.release()
            release_active_domain = False
            async with self._lock:
                if self._inflight[domain] > 0:
                    self._inflight[domain] -= 1
                if self._inflight[domain] <= 0:
                    self._inflight.pop(domain, None)
                if self._domain_refs[domain] > 0:
                    self._domain_refs[domain] -= 1
                if self._domain_refs[domain] <= 0:
                    self._domain_refs.pop(domain, None)
                    if domain in self._active_domains:
                        self._active_domains.discard(domain)
                        release_active_domain = True
            if release_active_domain:
                self._active_domain_sem.release()


async def record_domain_pacing_event(
    pool: Any,
    *,
    source: str,
    event_type: str,
    url: str | None = None,
    status_code: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Best-effort telemetry write; never blocks collection correctness."""
    if pool is None:
        return
    target_url = url or ""
    domain = registrable_domain_from_url(target_url)
    host = host_from_url(target_url) or None
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector_domain_pacing_events
                    (source, registrable_domain, host, event_type, url, status_code, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                """,
                source,
                domain,
                host,
                event_type,
                target_url or None,
                status_code,
                json.dumps(metadata or {}, default=str),
            )
    except Exception:
        return
