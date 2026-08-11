from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

from src.dashboard import api as dashboard_api  # noqa: E402


class _Acquire:
    def __init__(self, conn: "_FakeConn"):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return None


class _FakePool:
    def __init__(self, conn: "_FakeConn"):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.fetch_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.rows


def _patch_pool(monkeypatch: pytest.MonkeyPatch, conn: _FakeConn) -> None:
    async def fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)


@pytest.mark.asyncio
async def test_recon_observations_returns_joined_rows(monkeypatch):
    observed_at = datetime(2026, 8, 11, 4, 30, tzinfo=timezone.utc)
    conn = _FakeConn([
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "target_id": "22222222-2222-2222-2222-222222222222",
            "target_type": "domain",
            "target_value": "example.com",
            "module": "sfp_dnsresolve",
            "observation_type": "DOMAIN_NAME",
            "value": "www.example.com",
            "confidence": 0.8,
            "first_seen_at": observed_at,
            "last_seen_at": observed_at,
        }
    ])
    _patch_pool(monkeypatch, conn)

    out = await dashboard_api.recon_observations(
        target_id="22222222-2222-2222-2222-222222222222",
        limit=25,
        _user={},
    )

    assert out["total"] == 1
    assert out["limit"] == 25
    assert out["observations"][0]["target_value"] == "example.com"
    query, args = conn.fetch_calls[0]
    assert "JOIN recon_targets" in query
    assert "ORDER BY o.last_seen_at DESC" in query
    assert args == ("22222222-2222-2222-2222-222222222222", 25)


@pytest.mark.asyncio
async def test_recon_observations_rejects_invalid_target_id(monkeypatch):
    conn = _FakeConn([])
    _patch_pool(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc:
        await dashboard_api.recon_observations(target_id="not-a-uuid", _user={})

    assert exc.value.status_code == 400
    assert exc.value.detail == "invalid target_id"
    assert conn.fetch_calls == []
