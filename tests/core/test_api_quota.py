from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.core.api_quota import target_units, upsert_api_quota_snapshot


def test_target_units_clamps_ratio():
    assert target_units(1000, 0.9) == 900
    assert target_units(1000, 2.0) == 1000
    assert target_units(1000, -1.0) == 0


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
    async def execute(self, query, *args):
        self.query = query
        self.args = args


@pytest.mark.asyncio
async def test_upsert_api_quota_snapshot_serializes_jsonb_metadata():
    conn = _Conn()
    await upsert_api_quota_snapshot(
        _Pool(conn),
        service="youtube",
        account="api_key:test",
        bucket="search",
        quota_date=date(2026, 8, 13),
        reset_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
        used_units=100,
        quota_units=1000,
        target_ratio=0.9,
        metadata={"endpoint": "search.list"},
    )

    assert "$12::jsonb" in conn.query
    assert conn.args[0:4] == ("youtube", "api_key:test", "search", date(2026, 8, 13))
    assert conn.args[5:9] == (100, 900, 1000, 900)
    assert conn.args[-1] == '{"endpoint": "search.list"}'
