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


def _cancel_test_pacer(**overrides):
    options = dict(
        source="test",
        env_prefix="TEST_DOMAIN_PACING_CANCEL",
        max_active_domains=1,
        max_per_domain=2,
        delay_seconds=5.0,
        jitter_seconds=0.0,
    )
    options.update(overrides)
    return DomainPacer(**options)


async def _enter_slot(pacer: DomainPacer, url: str) -> None:
    async with pacer.slot(url):
        return None


@pytest.mark.asyncio
async def test_slot_cancelled_during_delay_releases_domain_permit(monkeypatch):
    pacer = _cancel_test_pacer()
    sleeping = asyncio.Event()

    async def fake_sleep(_delay: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    sem = await pacer._domain_sem("a.example")
    baseline = sem._value

    task = asyncio.create_task(_enter_slot(pacer, "https://a.example/1"))
    await sleeping.wait()

    assert sem._value == baseline - 1
    assert pacer.snapshot().active_domains == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sem._value == baseline
    snapshot = pacer.snapshot()
    assert snapshot.active_domains == 0
    assert snapshot.per_domain_inflight == {}


@pytest.mark.asyncio
async def test_slot_cancelled_while_waiting_for_active_domain_permit():
    pacer = _cancel_test_pacer(max_active_domains=1, delay_seconds=0.0)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with pacer.slot("https://a.example/1"):
            holding.set()
            await release.wait()

    first = asyncio.create_task(hold())
    await holding.wait()
    active_sem = pacer._active_domain_sem
    assert active_sem._value == 0

    second = asyncio.create_task(_enter_slot(pacer, "https://b.example/1"))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not second.done()

    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second

    assert active_sem._value == 0
    assert pacer.snapshot().active_domains == 1

    release.set()
    await first
    assert active_sem._value == 1
    assert pacer.snapshot().active_domains == 0


@pytest.mark.asyncio
async def test_repeated_slot_cancellations_do_not_wedge_pacer(monkeypatch):
    pacer = _cancel_test_pacer(max_active_domains=2, max_per_domain=1)
    sleeping = asyncio.Event()

    async def fake_sleep(_delay: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    for index in range(6):
        url = f"https://d{index}.example/1"
        sleeping.clear()
        task = asyncio.create_task(_enter_slot(pacer, url))
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        snapshot = pacer.snapshot()
        assert snapshot.active_domains == 0
        domain = registrable_domain_from_url(url)
        assert pacer._domain_sems[domain]._value == pacer.max_per_domain

    assert pacer._active_domain_sem._value == pacer.max_active_domains

    pacer.delay_seconds = 0.0
    async with pacer.slot("https://d0.example/1"):
        assert pacer.snapshot().active_domains == 1
    assert pacer.snapshot().active_domains == 0
    assert pacer._active_domain_sem._value == pacer.max_active_domains
