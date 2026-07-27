from __future__ import annotations

import os

import pytest

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

from src.dashboard.api import _safe_fetch_int


class FakeConn:
    def __init__(self, value=None, error: Exception | None = None):
        self.value = value
        self.error = error

    async def fetchval(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return self.value


@pytest.mark.asyncio
async def test_safe_fetch_int_returns_default_on_query_error():
    assert await _safe_fetch_int(FakeConn(error=TimeoutError()), "select 1", default=7) == 7


@pytest.mark.asyncio
async def test_safe_fetch_int_coerces_value():
    assert await _safe_fetch_int(FakeConn(value="12"), "select 1") == 12
