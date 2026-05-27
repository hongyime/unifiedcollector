"""Tests for POST /targets dedupe behaviour against spider graph data.

Gated on SPIDER_TEST_DSN env var (skipped when unset). Lets the real
src.db.connection.get_pool create the pool inside the FastAPI lifespan,
so we don't fight asyncpg's loop affinity.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.environ.get("SPIDER_TEST_DSN")
if not DSN:
    pytest.skip("SPIDER_TEST_DSN not set", allow_module_level=True)

# Point the real get_pool at the test DSN before importing the app.
os.environ["DATABASE_URL"] = DSN
os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

import asyncpg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.dashboard.api import app, get_current_user  # noqa: E402

TAG = f"dedupetest_{uuid.uuid4().hex[:8]}"
NEW_USER = f"newuser_{TAG}"
KNOWN_USER = f"knownuser_{TAG}"
SEEDED_USER = f"seeded_{TAG}"
PARENT_USER = f"parent_{TAG}"
YT_TARGET = f"ytchan_{TAG}"
SEARCH_TARGET = f"searchq_{TAG}"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _seed_sync():
    """Seed test rows via a one-shot connection (sync wrapper)."""
    async def _go():
        conn = await asyncpg.connect(DSN)
        try:
            await _cleanup(conn)
            await conn.execute(
                "INSERT INTO instagram_profiles (username, platform_user_id) "
                "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                KNOWN_USER, f"pid_{KNOWN_USER}",
            )
            await conn.execute(
                "INSERT INTO instagram_profiles (username, platform_user_id) "
                "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                SEEDED_USER, f"pid_{SEEDED_USER}",
            )
            await conn.execute(
                "INSERT INTO spider_queue (platform, node_id, parent_node_id, status) "
                "VALUES ($1, $2, $3, 'completed') ON CONFLICT DO NOTHING",
                "instagram", SEEDED_USER, PARENT_USER,
            )
        finally:
            await conn.close()
    asyncio.run(_go())


def _cleanup_sync():
    async def _go():
        conn = await asyncpg.connect(DSN)
        try:
            await _cleanup(conn)
        finally:
            await conn.close()
    asyncio.run(_go())


async def _cleanup(conn):
    await conn.execute(
        "DELETE FROM collection_targets WHERE target_id LIKE $1", f"%{TAG}%"
    )
    await conn.execute(
        "DELETE FROM instagram_profiles WHERE username LIKE $1", f"%{TAG}%"
    )
    await conn.execute(
        "DELETE FROM spider_queue WHERE node_id LIKE $1 OR parent_node_id LIKE $1",
        f"%{TAG}%",
    )


def _read_priority_sync(source, target_id):
    async def _go():
        conn = await asyncpg.connect(DSN)
        try:
            return await conn.fetchval(
                "SELECT priority FROM collection_targets WHERE source=$1 AND target_id=$2",
                source, target_id,
            )
        finally:
            await conn.close()
    return asyncio.run(_go())


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
def _seed_module():
    _seed_sync()
    yield
    _cleanup_sync()


@pytest.fixture
def client():
    # Reset the connection-pool singleton so each test (in its own
    # TestClient loop) gets a fresh pool bound to the right loop.
    import asyncio as _asyncio
    from src.db import connection as _dbconn
    _dbconn._pool = None
    _dbconn._pool_lock = _asyncio.Lock()

    async def _fake_user():
        return {"username": "tester", "role": "admin"}
    app.dependency_overrides[get_current_user] = _fake_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _dbconn._pool = None


def _post_target(client, source, target, priority=0, force=False):
    url = "/targets"
    if force:
        url += "?force=true"
    return client.post(url, json={"source": source, "target": target, "priority": priority})


# ── Tests ───────────────────────────────────────────────────────────────────


def test_post_fresh_instagram_target_succeeds(client):
    r = _post_target(client, "instagram", NEW_USER, priority=1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["source"] == "instagram"
    assert body["target"] == NEW_USER


def test_post_known_instagram_target_returns_409(client):
    r = _post_target(client, "instagram", KNOWN_USER, priority=1)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "already_discovered"
    assert detail["source"] == "instagram"
    assert detail["target_id"] == KNOWN_USER
    assert "discovered_via" in detail
    assert "last_seen" in detail


def test_force_known_target_succeeds_and_bumps_priority(client):
    r1 = _post_target(client, "instagram", KNOWN_USER, priority=2)
    assert r1.status_code == 409

    r2 = _post_target(client, "instagram", KNOWN_USER, priority=2, force=True)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body.get("forced") is True

    prio = _read_priority_sync("instagram", KNOWN_USER)
    assert prio == 7, f"expected priority bumped to 7, got {prio}"


def test_post_youtube_no_dedupe_table_succeeds(client):
    r = _post_target(client, "youtube", YT_TARGET)
    assert r.status_code == 200, r.text


def test_post_search_freeform_succeeds(client):
    r = _post_target(client, "search", SEARCH_TARGET)
    assert r.status_code == 200, r.text


def test_post_seeded_with_parent_returns_discovered_via(client):
    r = _post_target(client, "instagram", SEEDED_USER)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "already_discovered"
    assert detail["target_id"] == SEEDED_USER
    assert detail["discovered_via"] == PARENT_USER
