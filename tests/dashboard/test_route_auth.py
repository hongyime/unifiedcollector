"""Per-route auth regression: QR/link and social routes must require a viewer.

These routes previously shipped unauthenticated. With auth enabled (the
default) a request without a bearer token must be rejected before the handler
runs, including the HTML pages.
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")
# Auth-enabled is the default; make it explicit so a local shell cannot disable it.
os.environ.pop("DASHBOARD_AUTH_DISABLED", None)

from fastapi.testclient import TestClient  # noqa: E402

from src.dashboard.api import app  # noqa: E402
from src.dashboard.api import auth as dashboard_auth  # noqa: E402

_ROUTES = [
    "/whatsapp/qr/1",
    "/whatsapp/link",
    "/social/stats",
    "/social/users",
    "/social/scrape-config",
    "/social",
]


@pytest.mark.parametrize("path", _ROUTES)
def test_route_rejects_missing_token(path):
    # TestClient without a with-block skips lifespan; these handlers need no DB
    # before auth is evaluated.
    client = TestClient(app)
    resp = client.get(path)
    assert resp.status_code == 401, f"{path} -> {resp.status_code}"


def test_auth_disabled_still_treats_requests_as_admin(monkeypatch):
    """Documented escape hatch: DASHBOARD_AUTH_DISABLED bypasses token checks."""
    monkeypatch.setattr(dashboard_auth, "_AUTH_DISABLED", True)
    user = asyncio.run(dashboard_auth.get_current_user(creds=None))
    assert user == {"username": "localhost", "role": "admin"}
    check = dashboard_auth.require_role("viewer")
    assert asyncio.run(check(user)) == user
