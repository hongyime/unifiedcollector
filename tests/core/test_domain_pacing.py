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
