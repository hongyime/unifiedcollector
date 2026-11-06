from __future__ import annotations

import pytest

from src.core.schedule_status import set_collection_schedule


class _Conn:
    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_set_collection_schedule_bounds_interval_and_preserves_enabled_state():
    pool = _Pool()

    result = await set_collection_schedule(pool, "telegram", interval_hours=999, enabled=False)

    assert result["interval_hours"] == 720
    assert result["enabled"] is False
    query, args = pool.conn.calls[0]
    assert "ON CONFLICT (source) DO UPDATE" in query
    assert args[0:3] == ("telegram", 720, False)
