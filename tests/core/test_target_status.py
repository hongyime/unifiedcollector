from __future__ import annotations

import pytest

from src.core.target_status import TargetAlreadyKnown, add_collection_target


class _Conn:
    def __init__(self, known=False):
        self.known = known
        self.calls = []

    async def fetchrow(self, _query, *_args):
        return {"hit": "known"} if self.known else None

    async def fetchval(self, *_args):
        return None

    async def execute(self, query, *args):
        self.calls.append((query, args))


class _Pool:
    def __init__(self, known=False):
        self.conn = _Conn(known)

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_add_collection_target_uses_safe_default_and_force_is_explicit():
    pool = _Pool()

    result = await add_collection_target(pool, "youtube", "channel", priority=2)

    assert result == {"source": "youtube", "target": "channel", "forced": False}
    assert pool.conn.calls[0][1] == ("youtube", "channel", 2)


@pytest.mark.asyncio
async def test_add_collection_target_rejects_known_profile_without_force():
    with pytest.raises(TargetAlreadyKnown):
        await add_collection_target(_Pool(known=True), "instagram", "known")
