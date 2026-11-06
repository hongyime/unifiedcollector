from __future__ import annotations

import pytest

from src.core.telegram_account_controls import set_telegram_account_enabled


class _Conn:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return self.result


class _Pool:
    def __init__(self, result):
        self.conn = _Conn(result)

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_enable_notifies_only_after_a_real_state_change():
    pool = _Pool("UPDATE 1")

    assert await set_telegram_account_enabled(pool, "account", enabled=True) is True
    assert "status='active'" in pool.conn.calls[0][0]
    assert "pg_notify" in pool.conn.calls[1][0]


@pytest.mark.asyncio
async def test_disable_never_notifies_and_reports_missing_account():
    pool = _Pool("UPDATE 0")

    assert await set_telegram_account_enabled(pool, "account", enabled=False) is False
    assert len(pool.conn.calls) == 1
    assert "status='disabled'" in pool.conn.calls[0][0]
