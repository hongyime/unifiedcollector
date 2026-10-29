"""GET /api/source-controls must be mounted and viewer-gated.

Before the router was mounted this path was a 404 (and a 200 HTML SPA fallback
when the frontend bundle exists); with the default auth-enabled config an
anonymous request must be 401 and a viewer token must reach the handler.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")
os.environ.pop("DASHBOARD_AUTH_DISABLED", None)

import jwt  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.dashboard.api import app  # noqa: E402
from src.dashboard.api import auth as dashboard_auth  # noqa: E402
from src.dashboard.api import source_controls as source_controls_module  # noqa: E402

_PATH = "/api/source-controls"
_SEEN_AT = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
_ROW = {
    "source_name": "instagram",
    "state": "RATE_LIMITED",
    "enabled": True,
    "last_success": _SEEN_AT,
    "last_attempt": None,
    "blocked_until": None,
    "stop_reason": "HTTP 429",
    "consecutive_failures": 3,
    "updated_at": _SEEN_AT,
}


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, sql, *args):
        return self._rows


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    @asynccontextmanager
    async def acquire(self):
        yield _FakeConn(self._rows)


def _viewer_token() -> str:
    return jwt.encode(
        {
            "sub": "viewer-user",
            "role": "viewer",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        dashboard_auth.JWT_SECRET,
        algorithm="HS256",
    )


def test_route_is_mounted():
    assert any(getattr(route, "path", None) == _PATH for route in app.routes)


def test_route_requires_a_token():
    assert TestClient(app).get(_PATH).status_code == 401


def test_route_returns_rows_for_a_viewer(monkeypatch):
    async def fake_get_pool():
        return _FakePool([_ROW])

    monkeypatch.setattr(source_controls_module, "get_pool", fake_get_pool)
    client = TestClient(app)
    resp = client.get(_PATH, headers={"Authorization": f"Bearer {_viewer_token()}"})

    assert resp.status_code == 200
    rows = resp.json()
    assert rows == [
        {
            "source_name": "instagram",
            "state": "RATE_LIMITED",
            "enabled": True,
            "last_success": _SEEN_AT.isoformat(),
            "last_attempt": None,
            "blocked_until": None,
            "stop_reason": "HTTP 429",
            "consecutive_failures": 3,
            "updated_at": _SEEN_AT.isoformat(),
        }
    ]
