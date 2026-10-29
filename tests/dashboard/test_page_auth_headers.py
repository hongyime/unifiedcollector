"""Server-rendered HTML pages must send the bearer token once the SPA logs in.

The WhatsApp link page and the social registry page are inline HTML served by
the dashboard, not part of the SPA bundle. When auth is enabled their fetches
must carry ``Authorization: Bearer <token>`` from ``localStorage`` just like
``frontend/src/services/api.ts`` does. With auth disabled ``authHeaders()``
returns ``{}`` and the pages behave exactly as before.
"""
from __future__ import annotations

import asyncio
import os
import re

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

from src.dashboard.api import social_page, whatsapp_link_page  # noqa: E402


def _html(handler) -> str:
    resp = asyncio.run(handler(_user={}))
    return resp.body.decode("utf-8")


_PAGES = {
    "whatsapp_link": lambda: _html(whatsapp_link_page),
    "social": lambda: _html(social_page),
}


def test_templates_define_auth_helper_reading_localstorage_token():
    for name, get_html in _PAGES.items():
        html = get_html()
        assert "function authHeaders()" in html, name
        assert "auth_token" in html, name
        assert "Bearer " in html, name
        assert "Authorization" in html, name


def test_every_fetch_passes_auth_headers():
    # Make a future fetch impossible to ship silently: each ``fetch(`` in the
    # page must be accompanied by an ``authHeaders()`` call.
    for name, get_html in _PAGES.items():
        html = get_html()
        fetches = html.count("fetch(")
        authed = len(re.findall(r"(?<!function )authHeaders\(\)", html))
        assert fetches > 0, name
        assert fetches == authed, f"{name}: {fetches} fetch() vs {authed} authHeaders()"
