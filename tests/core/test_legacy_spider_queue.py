import asyncio
from contextlib import asynccontextmanager

import pytest

from src.core import legacy_spider_queue


class _Conn:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.mark.asyncio
async def test_legacy_spider_lease_heartbeat_is_fenced(monkeypatch):
    pool = _Pool()
    monkeypatch.setattr(legacy_spider_queue, "LEASE_HEARTBEAT_SECONDS", 0.001)

    async with legacy_spider_queue.maintain_legacy_spider_lease(
        pool, "youtube_spider_queue", "row-1", "claim-1",
    ):
        await asyncio.sleep(0.01)

    assert pool.conn.calls
    sql, args = pool.conn.calls[0]
    assert "lease_expires_at = NOW()" in sql
    assert "status = 'processing' AND claim_token = $2" in sql
    assert args == ("row-1", "claim-1")


@pytest.mark.asyncio
async def test_legacy_spider_lease_rejects_unknown_table():
    with pytest.raises(ValueError, match="unsupported legacy spider table"):
        async with legacy_spider_queue.maintain_legacy_spider_lease(None, "not_a_table", 1, "claim"):
            pass
