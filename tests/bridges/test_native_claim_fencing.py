"""Lease-fencing and durable-counting tests for native/browser scrape claims.

These tests run against a disposable PostgreSQL 16 container
(``tests/recovery/pg_harness``) so the real predicates are exercised — the
``claim_token`` compare-and-swap on completion, the ``FOR UPDATE`` claim check
in the browser ingest endpoint, and the ``ON CONFLICT`` idempotency of the
content upserts — rather than a fake connection that re-implements them.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import asyncpg
import pytest

from src.collectors.native_platform_scrape import (
    claim_native_target,
    finish_native_target,
)

_REPO = Path(__file__).resolve().parents[2]
_MIGRATIONS = (
    "src/db/migrations/20261014_add_native_platform_scraping.sql",
    "src/db/migrations/20261014_add_snapchat_spotlight_highlight_tables.sql",
    "src/db/migrations/20261023_add_native_claim_tokens.sql",
)
_TRUNCATE_TABLES = (
    "browser_native_scrape_queue, pinterest_pins, pinterest_boards, "
    "snapchat_stories, snapchat_spotlights, snapchat_highlights, "
    "airbnb_listings, airbnb_hosts"
)


@pytest.fixture(scope="module")
def _disposable_pg():
    from tests.recovery.pg_harness import (
        DisposablePostgres,
        assert_environment_safe,
        resolve_external_dsn,
    )

    external_dsn = resolve_external_dsn()
    assert_environment_safe(external_dsn or "")
    if external_dsn is not None:
        harness = DisposablePostgres.external(external_dsn)
    else:
        harness = DisposablePostgres.start()
        assert_environment_safe(harness.dsn)

    async def _apply_schema() -> None:
        conn = await asyncpg.connect(harness.driver_dsn)
        try:
            for name in _MIGRATIONS:
                await conn.execute((_REPO / name).read_text())
        finally:
            await conn.close()

    asyncio.run(_apply_schema())
    yield harness
    if harness.owned:
        harness.stop()


@pytest.fixture
async def _pg_pool(_disposable_pg):
    pool = await asyncpg.create_pool(_disposable_pg.driver_dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(f"TRUNCATE {_TRUNCATE_TABLES}")
        yield pool
        async with pool.acquire() as conn:
            await conn.execute(f"TRUNCATE {_TRUNCATE_TABLES}")
    finally:
        await pool.close()


async def _seed_target(pool, url="https://www.pinterest.com/alice/"):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO browser_native_scrape_queue (platform, target_url, username, source)
            VALUES ('pinterest', $1, 'alice', 'followed_handle_candidate')
            RETURNING id
            """,
            url,
        )


async def _queue_row(pool, target_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT id, status, attempts, claim_token, claim_expires_at, last_error
              FROM browser_native_scrape_queue WHERE id=$1
            """,
            target_id,
        )


async def _expire_lease(pool, target_id):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE browser_native_scrape_queue SET claim_expires_at=now()-interval '1 second' WHERE id=$1",
            target_id,
        )


async def _count(pool, table):
    async with pool.acquire() as conn:
        return await conn.fetchval(f"SELECT count(*) FROM {table}")


def _pin(pin_id):
    return {
        "type": "pin",
        "id": pin_id,
        "url": f"https://www.pinterest.com/pin/{pin_id}/",
        "username": "alice",
        "board_id": "alice:travel",
        "board_url": "https://www.pinterest.com/alice/travel/",
    }


class _FakeRequest:
    def __init__(self, body):
        self._body = body
        self._cache = {}

    def get(self, key, default=None):
        return self._cache.get(key, default)

    def __setitem__(self, key, value):
        self._cache[key] = value

    async def json(self):
        return self._body


async def _post_ingest(pool, body, monkeypatch):
    import src.bridges.ig_ingest as bridge
    from src.bridges.ig_ingest.native_scrape import native_scrape_ingest

    async def _fake_get_pool():
        return pool

    monkeypatch.setattr(bridge, "get_pool", _fake_get_pool)
    return await native_scrape_ingest(_FakeRequest(body))


def _ingest_body(**overrides):
    body = {
        "platform": "pinterest",
        "page_url": "https://www.pinterest.com/alice/",
        "status": "success",
        "items": [_pin("111")],
    }
    body.update(overrides)
    return body


# ── native collector claim/complete fencing ───────────────────────────────


async def test_claim_issues_unique_token_and_completion_is_cas(_pg_pool):
    pool = _pg_pool
    target_id = await _seed_target(pool)

    claim = await claim_native_target(pool, "pinterest")
    assert claim is not None
    assert claim["id"] == target_id
    token = claim["claim_token"]
    assert isinstance(token, uuid.UUID)

    row = await _queue_row(pool, target_id)
    assert row["status"] == "claimed"
    assert row["claim_token"] == token

    assert await finish_native_target(
        pool, "pinterest", target_id, claim_token=token, success=True,
    ) is True
    row = await _queue_row(pool, target_id)
    assert row["status"] == "completed"
    assert row["claim_token"] == token


async def test_two_worker_race_only_current_token_completes(_pg_pool):
    pool = _pg_pool
    target_id = await _seed_target(pool)

    stale = (await claim_native_target(pool, "pinterest"))["claim_token"]
    await _expire_lease(pool, target_id)
    current = (await claim_native_target(pool, "pinterest"))["claim_token"]
    assert current != stale

    # Worker A (expired lease) must not complete worker B's claim.
    assert await finish_native_target(
        pool, "pinterest", target_id, claim_token=stale, success=True,
    ) is False
    row = await _queue_row(pool, target_id)
    assert row["status"] == "claimed"
    assert row["claim_token"] == current
    assert row["claim_expires_at"] is not None

    # Worker A must not fail worker B's claim either.
    assert await finish_native_target(
        pool, "pinterest", target_id, claim_token=stale,
        success=False, error="late_worker",
    ) is False
    row = await _queue_row(pool, target_id)
    assert row["status"] == "claimed"
    assert row["last_error"] is None

    # Worker B (current token) completes normally.
    assert await finish_native_target(
        pool, "pinterest", target_id, claim_token=current, success=True,
    ) is True
    row = await _queue_row(pool, target_id)
    assert row["status"] == "completed"


async def test_stale_or_malformed_token_is_clean_rejection(_pg_pool):
    pool = _pg_pool
    target_id = await _seed_target(pool)
    token = (await claim_native_target(pool, "pinterest"))["claim_token"]

    assert await finish_native_target(
        pool, "pinterest", target_id, claim_token=uuid.uuid4(), success=True,
    ) is False
    row = await _queue_row(pool, target_id)
    assert row["status"] == "claimed"
    assert row["claim_token"] == token
    assert row["attempts"] == 1

    assert await finish_native_target(
        pool, "pinterest", target_id, claim_token="not-a-uuid", success=False,
    ) is False
    row = await _queue_row(pool, target_id)
    assert row["status"] == "claimed"
    assert row["last_error"] is None


# ── browser ingest (extension) claim/accept fencing ───────────────────────


async def test_browser_ingest_completes_only_with_current_token(_pg_pool, monkeypatch):
    pool = _pg_pool
    target_id = await _seed_target(pool)
    stale = (await claim_native_target(pool, "pinterest"))["claim_token"]
    await _expire_lease(pool, target_id)
    current = (await claim_native_target(pool, "pinterest"))["claim_token"]

    response = await _post_ingest(pool, _ingest_body(
        target_id=target_id, claim_token=str(stale),
    ), monkeypatch)
    assert response.status == 409
    assert "stale_claim" in response.text
    assert await _count(pool, "pinterest_pins") == 0
    row = await _queue_row(pool, target_id)
    assert row["status"] == "claimed"
    assert row["claim_token"] == current

    response = await _post_ingest(pool, _ingest_body(target_id=target_id), monkeypatch)
    assert response.status == 409
    row = await _queue_row(pool, target_id)
    assert row["status"] == "claimed"

    response = await _post_ingest(pool, _ingest_body(
        target_id=target_id, claim_token=str(current),
    ), monkeypatch)
    assert response.status == 200
    assert json.loads(response.text)["accepted"]["pin"] == 1
    assert await _count(pool, "pinterest_pins") == 1
    row = await _queue_row(pool, target_id)
    assert row["status"] == "completed"


async def test_browser_ingest_rejects_malformed_claim_token(_pg_pool, monkeypatch):
    pool = _pg_pool
    target_id = await _seed_target(pool)
    await claim_native_target(pool, "pinterest")

    response = await _post_ingest(pool, _ingest_body(
        target_id=target_id, claim_token="garbage-token",
    ), monkeypatch)
    assert response.status == 409
    assert await _count(pool, "pinterest_pins") == 0


# ── duplicate-delivery idempotency and counter-vs-row consistency ─────────


async def test_duplicate_delivery_creates_no_second_row_and_counters_match_rows(
    _pg_pool, monkeypatch,
):
    pool = _pg_pool
    body = _ingest_body(items=[
        _pin("111"),
        _pin("222"),
        # Rejected by validation (id fails _ID_RE): never counted, never stored.
        {"type": "pin", "id": "bad/id", "url": "https://www.pinterest.com/pin/333/"},
    ])

    response = await _post_ingest(pool, body, monkeypatch)
    assert response.status == 200
    first = json.loads(response.text)
    assert first["accepted"]["pin"] == 2
    assert await _count(pool, "pinterest_pins") == 2
    assert first["accepted"]["pin"] == await _count(pool, "pinterest_pins")

    response = await _post_ingest(pool, body, monkeypatch)
    assert response.status == 200
    second = json.loads(response.text)
    assert second["accepted"]["pin"] == 2
    # Repeated delivery updates in place: exactly one row per pin.
    assert await _count(pool, "pinterest_pins") == 2


async def test_counters_match_durable_rows_per_kind(_pg_pool, monkeypatch):
    pool = _pg_pool
    body = _ingest_body(items=[
        _pin("111"),
        _pin("222"),
        {
            "type": "board",
            "id": "alice:travel",
            "url": "https://www.pinterest.com/alice/travel/",
            "username": "alice",
            "name": "Travel",
        },
        {"type": "pin", "id": "bad/id", "url": "https://www.pinterest.com/pin/333/"},
    ])

    response = await _post_ingest(pool, body, monkeypatch)
    assert response.status == 200
    accepted = json.loads(response.text)["accepted"]
    assert accepted["pin"] == await _count(pool, "pinterest_pins") == 2
    assert accepted["board"] == await _count(pool, "pinterest_boards") == 1
