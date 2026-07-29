from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

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
        self.fetchval_calls = []

    async def fetchval(self, query, *args, **kwargs):
        self.fetchval_calls.append((query, args, kwargs))
        if "to_regclass('wa_discovered_links')" in query:
            return "wa_discovered_links"
        return None

    async def fetch(self, query, *args, **kwargs):
        self.fetch_calls.append((query, args, kwargs))
        return self.rows


def _patch_pool(monkeypatch: pytest.MonkeyPatch, conn: _FakeConn) -> None:
    async def fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)


@pytest.mark.asyncio
async def test_list_wa_links_returns_url_and_legacy_link(monkeypatch):
    conn = _FakeConn([
        {
            "id": 42,
            "chat_id": "58cdbf8d-f4c9-4d5f-9675-790826ae3349",
            "message_id": None,
            "url": "url",
            "source_jid": "120363000000@g.us",
            "domain": "chat.whatsapp.com",
            "link_type": "https://chat.whatsapp.com/InviteCode",
            "status": "pending",
            "title": None,
            "description": None,
            "thumbnail_url": None,
            "discovered_at": datetime(2026, 7, 29, tzinfo=timezone.utc),
            "fetched_at": None,
            "metadata": None,
        }
    ])
    _patch_pool(monkeypatch, conn)

    rows = await dashboard_api.list_wa_links()

    assert rows[0]["url"] == "https://chat.whatsapp.com/InviteCode"
    assert rows[0]["link"] == "https://chat.whatsapp.com/InviteCode"
    assert rows[0]["link_type"] == "url"
    assert rows[0]["source_jid"] == "120363000000@g.us"
    assert "l.url AS link" in conn.fetch_calls[0][0]
    assert "l.link_type AS _raw_link_type" in conn.fetch_calls[0][0]


@pytest.mark.asyncio
async def test_list_wa_links_accepts_legacy_filter_values(monkeypatch):
    conn = _FakeConn([])
    _patch_pool(monkeypatch, conn)

    await dashboard_api.list_wa_links(link_type="invite", status="new", limit=25)

    query, args, _kwargs = conn.fetch_calls[0]
    assert "l.link_type ILIKE 'http://%'" in query
    assert "= ANY($1::text[])" in query
    assert "l.status = ANY($2::text[])" in query
    assert args == (["group_invite", "group_invite_restricted"], ["pending"], 25)


def test_whatsapp_dashboard_columns_migration_is_additive():
    migration = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "db"
        / "migrations"
        / "add_whatsapp_dashboard_columns.sql"
    )
    sql = migration.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())

    assert "ALTER TABLE whatsapp_chats ADD COLUMN IF NOT EXISTS chat_type TEXT" in normalized
    assert "ALTER COLUMN chat_type SET DEFAULT 'dm'" in normalized
    assert "ALTER TABLE whatsapp_users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()" in normalized
    assert "DROP " not in sql.upper()
    assert "DELETE " not in sql.upper()


def test_whatsapp_link_type_swap_migration_repairs_only_swapped_rows():
    migration = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "db"
        / "migrations"
        / "fix_wa_discovered_link_type_swaps.sql"
    )
    sql = migration.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())

    assert "UPDATE wa_discovered_links" in normalized
    assert "WHERE url = 'url'" in normalized
    assert "link_type ~* '^https?://'" in normalized
    assert "link_type = 'url'" in normalized
    assert "DROP " not in sql.upper()
    assert "DELETE " not in sql.upper()
