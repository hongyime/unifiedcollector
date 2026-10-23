from __future__ import annotations

import asyncio

import pytest

from src.core.domain_pacing import (
    DomainPacer,
    host_from_url,
    record_domain_pacing_event,
    registrable_domain_from_url,
    round_robin_by_domain,
)


def test_domain_helpers_normalize_without_widening_subdomains():
    assert host_from_url("https://www.example.com/path") == "example.com"
    assert registrable_domain_from_url("https://x.example.com/path") == "x.example.com"


def test_round_robin_by_domain_interleaves_hosts():
    ordered = round_robin_by_domain([
        "https://a.example/1",
        "https://a.example/2",
        "https://b.example/1",
        "https://a.example/3",
        "https://b.example/2",
    ])
    assert ordered == [
        "https://a.example/1",
        "https://b.example/1",
        "https://a.example/2",
        "https://b.example/2",
        "https://a.example/3",
    ]


@pytest.mark.asyncio
async def test_domain_pacer_caps_active_domains():
    pacer = DomainPacer(
        "test",
        env_prefix="TEST_DOMAIN_PACING",
        max_active_domains=1,
        max_per_domain=2,
        delay_seconds=0,
        jitter_seconds=0,
    )
    entered: list[str] = []
    release_first = asyncio.Event()

    async def hold(url: str) -> None:
        async with pacer.slot(url):
            entered.append(url)
            if len(entered) == 1:
                await release_first.wait()

    first = asyncio.create_task(hold("https://a.example/1"))
    await asyncio.sleep(0)
    second = asyncio.create_task(hold("https://b.example/1"))
    await asyncio.sleep(0.02)

    assert entered == ["https://a.example/1"]
    assert pacer.snapshot().active_domains == 1

    release_first.set()
    await asyncio.gather(first, second)
    assert entered == ["https://a.example/1", "https://b.example/1"]


@pytest.mark.asyncio
async def test_cancel_while_holding_slot_releases_all_bookkeeping():
    pacer = DomainPacer(
        "test",
        env_prefix="TEST_DOMAIN_PACING",
        max_active_domains=1,
        max_per_domain=1,
        delay_seconds=0,
        jitter_seconds=0,
    )
    body_entered = False

    async def holder() -> None:
        nonlocal body_entered
        async with pacer.slot("https://a.example/1"):
            body_entered = True
            await asyncio.sleep(3600)

    task = asyncio.create_task(holder())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while pacer.snapshot().per_domain_inflight.get("a.example") != 1:
        assert loop.time() < deadline, "holder never acquired its slot"
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert body_entered is True
    snapshot = pacer.snapshot()
    assert snapshot.active_domains == 0
    assert snapshot.per_domain_inflight == {}
    assert dict(pacer._domain_refs) == {}
    assert dict(pacer._inflight) == {}
    assert pacer._active_domain_sem._value == 1


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_per_domain_slot_restores_refs():
    pacer = DomainPacer(
        "test",
        env_prefix="TEST_DOMAIN_PACING",
        max_active_domains=2,
        max_per_domain=1,
        delay_seconds=0,
        jitter_seconds=0,
    )
    release_first = asyncio.Event()

    async def hold(url: str, gate: asyncio.Event | None = None) -> None:
        async with pacer.slot(url):
            if gate is not None:
                await gate.wait()

    first = asyncio.create_task(hold("https://a.example/1", release_first))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while dict(pacer._domain_refs).get("a.example") != 1 or pacer.snapshot().per_domain_inflight.get("a.example") != 1:
        assert loop.time() < deadline, "holder never acquired its slot"
        await asyncio.sleep(0.01)

    waiter = asyncio.create_task(hold("https://a.example/2"))
    deadline = loop.time() + 2.0
    while dict(pacer._domain_refs).get("a.example") != 2:
        assert loop.time() < deadline, "waiter never registered its domain ref"
        await asyncio.sleep(0.01)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert dict(pacer._domain_refs) == {"a.example": 1}
    assert pacer.snapshot().per_domain_inflight == {"a.example": 1}
    assert pacer.snapshot().active_domains == 1

    release_first.set()
    await first
    snapshot = pacer.snapshot()
    assert snapshot.active_domains == 0
    assert snapshot.per_domain_inflight == {}
    assert dict(pacer._domain_refs) == {}
    assert dict(pacer._inflight) == {}
    assert pacer._active_domain_sem._value == 2


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_active_domain_slot_leaves_no_trace():
    pacer = DomainPacer(
        "test",
        env_prefix="TEST_DOMAIN_PACING",
        max_active_domains=1,
        max_per_domain=1,
        delay_seconds=0,
        jitter_seconds=0,
    )
    release_first = asyncio.Event()

    async def hold(url: str, gate: asyncio.Event | None = None) -> None:
        async with pacer.slot(url):
            if gate is not None:
                await gate.wait()

    first = asyncio.create_task(hold("https://a.example/1", release_first))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while pacer.snapshot().active_domains != 1:
        assert loop.time() < deadline, "holder never became the active domain"
        await asyncio.sleep(0.01)

    waiter = asyncio.create_task(hold("https://b.example/1"))
    deadline = loop.time() + 2.0
    while not pacer._active_domain_sem._waiters:
        assert loop.time() < deadline, "waiter never queued on the active-domain semaphore"
        await asyncio.sleep(0.01)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert pacer.snapshot().active_domains == 1
    assert dict(pacer._domain_refs) == {"a.example": 1}
    assert pacer._active_domain_sem._value == 0

    release_first.set()
    await first
    snapshot = pacer.snapshot()
    assert snapshot.active_domains == 0
    assert snapshot.per_domain_inflight == {}
    assert dict(pacer._domain_refs) == {}
    assert dict(pacer._inflight) == {}
    assert pacer._active_domain_sem._value == 1


@pytest.mark.asyncio
async def test_cancel_during_politeness_delay_releases_everything():
    pacer = DomainPacer(
        "test",
        env_prefix="TEST_DOMAIN_PACING",
        max_active_domains=1,
        max_per_domain=1,
        delay_seconds=0.2,
        jitter_seconds=0,
    )
    body_entered = False

    async def holder() -> None:
        nonlocal body_entered
        async with pacer.slot("https://a.example/1"):
            body_entered = True
            await asyncio.sleep(3600)

    task = asyncio.create_task(holder())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while dict(pacer._inflight).get("a.example") != 1:
        assert loop.time() < deadline, "slot never reached the in-flight bookkeeping"
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert body_entered is False
    snapshot = pacer.snapshot()
    assert snapshot.active_domains == 0
    assert snapshot.per_domain_inflight == {}
    assert dict(pacer._domain_refs) == {}
    assert dict(pacer._inflight) == {}
    assert pacer._active_domain_sem._value == 1


@pytest.mark.asyncio
async def test_cancel_burst_never_over_releases_or_underflows_counters():
    pacer = DomainPacer(
        "test",
        env_prefix="TEST_DOMAIN_PACING",
        max_active_domains=4,
        max_per_domain=1,
        delay_seconds=0,
        jitter_seconds=0,
    )
    release_first = asyncio.Event()

    async def hold(url: str, gate: asyncio.Event | None = None) -> None:
        async with pacer.slot(url):
            if gate is not None:
                await gate.wait()

    first = asyncio.create_task(hold("https://a.example/1", release_first))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while pacer.snapshot().per_domain_inflight.get("a.example") != 1:
        assert loop.time() < deadline, "holder never acquired its slot"
        await asyncio.sleep(0.01)

    waiters = [asyncio.create_task(hold(f"https://a.example/{i}")) for i in range(2, 5)]
    deadline = loop.time() + 2.0
    while dict(pacer._domain_refs).get("a.example") != 4:
        assert loop.time() < deadline, "waiters never registered their domain refs"
        await asyncio.sleep(0.01)

    for waiter in waiters:
        waiter.cancel()
    results = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)

    assert dict(pacer._domain_refs) == {"a.example": 1}
    assert pacer.snapshot().per_domain_inflight == {"a.example": 1}

    release_first.set()
    await first
    snapshot = pacer.snapshot()
    assert snapshot.active_domains == 0
    assert snapshot.per_domain_inflight == {}
    assert dict(pacer._domain_refs) == {}
    assert dict(pacer._inflight) == {}
    assert pacer._active_domain_sem._value == 4
    assert pacer._domain_sems["a.example"]._value == 1

    async with pacer.slot("https://a.example/9"):
        assert pacer.snapshot().per_domain_inflight.get("a.example") == 1
    assert pacer.snapshot().per_domain_inflight == {}
    assert pacer._active_domain_sem._value == 4
    assert pacer._domain_sems["a.example"]._value == 1


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return None


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    def __init__(self):
        self.args = None

    async def execute(self, query, *args):
        self.query = query
        self.args = args


@pytest.mark.asyncio
async def test_record_domain_pacing_event_serializes_metadata_for_jsonb():
    conn = _Conn()
    await record_domain_pacing_event(
        _Pool(conn),
        source="website",
        event_type="crawl_summary",
        url="https://www.example.com/a",
        metadata={"pages": 2},
    )
    assert "$7::jsonb" in conn.query
    assert conn.args[1] == "example.com"
    assert conn.args[-1] == '{"pages": 2}'
