"""Unit tests for the Sprint 5 WhatsApp contact staging path.

Tests cover:
1. WhatsappStagingMergeHandler.should_run — gating on WA_STAGING_ENABLED and interval
2. _merge_batch — empty staging table returns (0, 0) without executing inserts
3. _merge_batch — single user-JID event merges into whatsapp_users only
4. _merge_batch — N events with duplicate platform_user_id deduplicate on merge
5. _merge_batch — NULL name/pushname preserved via COALESCE (no overwrite with NULL)
6. _merge_batch — is_business OR-merge: False|True → True
7. _merge_batch — lid_map skip when lid or phone_jid is absent
8. _merge_batch — lid_map rows merged when both lid + phone_jid present

All tests are pure-unit (asyncpg replaced with AsyncMock / MagicMock).
No real DB required.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.scheduler.handlers.wa_staging_merge import (
    WhatsappStagingMergeHandler,
    _staging_enabled,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(*, staging_enabled="0", interval_ms=5000, batch_max=1000):
    ctx = MagicMock()
    ctx.get_env_int = MagicMock(side_effect=lambda key, default, min_value=None: {
        "WA_STAGING_MERGE_INTERVAL_MS": interval_ms,
        "WA_STAGING_BATCH_MAX": batch_max,
    }.get(key, default))

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    ctx.pool = pool
    ctx._conn = conn
    ctx._staging_enabled = staging_enabled
    return ctx


def _row(
    id=1,
    platform_user_id="6591234567@s.whatsapp.net",
    name="Alice",
    pushname="Alice P",
    phone_number="6591234567",
    is_business=False,
    lid=None,
    phone_jid=None,
    collected_at=None,
):
    return {
        "id": id,
        "platform_user_id": platform_user_id,
        "name": name,
        "pushname": pushname,
        "phone_number": phone_number,
        "is_business": is_business,
        "lid": lid,
        "phone_jid": phone_jid,
        "collected_at": collected_at or datetime(2026, 9, 10, 0, 0, 0, tzinfo=timezone.utc),
    }


# ---------------------------------------------------------------------------
# 1. should_run gating
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_should_run_false_when_staging_disabled(monkeypatch):
    monkeypatch.setenv("WA_STAGING_ENABLED", "0")
    handler = WhatsappStagingMergeHandler()
    ctx = _make_ctx(staging_enabled="0")
    assert await handler.should_run(ctx) is False


@pytest.mark.asyncio
async def test_should_run_false_when_interval_not_elapsed(monkeypatch):
    monkeypatch.setenv("WA_STAGING_ENABLED", "1")
    handler = WhatsappStagingMergeHandler()
    handler._last_run = time.monotonic()  # just ran
    ctx = _make_ctx(staging_enabled="1", interval_ms=5000)
    assert await handler.should_run(ctx) is False


@pytest.mark.asyncio
async def test_should_run_true_when_enabled_and_interval_elapsed(monkeypatch):
    monkeypatch.setenv("WA_STAGING_ENABLED", "1")
    handler = WhatsappStagingMergeHandler()
    handler._last_run = 0.0  # never ran
    ctx = _make_ctx(staging_enabled="1", interval_ms=5000)
    assert await handler.should_run(ctx) is True


def test_staging_enabled_helper(monkeypatch):
    monkeypatch.setenv("WA_STAGING_ENABLED", "1")
    assert _staging_enabled() is True
    monkeypatch.setenv("WA_STAGING_ENABLED", "0")
    assert _staging_enabled() is False
    monkeypatch.delenv("WA_STAGING_ENABLED", raising=False)
    assert _staging_enabled() is False


# ---------------------------------------------------------------------------
# 2. empty staging table → no inserts, returns (0, 0)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_batch_empty_returns_zero():
    ctx = _make_ctx()
    ctx._conn.fetch = AsyncMock(return_value=[])

    user_merged, lid_merged = await WhatsappStagingMergeHandler._merge_batch(
        ctx.pool, batch_max=1000
    )

    assert user_merged == 0
    assert lid_merged == 0
    # execute must NOT be called — nothing to merge
    ctx._conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# 3. single user-JID event → whatsapp_users merged, lid_map skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_batch_single_user_jid():
    ctx = _make_ctx()
    rows = [_row(id=1)]
    ctx._conn.fetch = AsyncMock(return_value=rows)

    user_merged, lid_merged = await WhatsappStagingMergeHandler._merge_batch(
        ctx.pool, batch_max=1000
    )

    assert user_merged == 1
    assert lid_merged == 0
    # execute called twice: INSERT whatsapp_users + DELETE
    assert ctx._conn.execute.call_count == 2
    first_call_sql = ctx._conn.execute.call_args_list[0][0][0]
    assert "INSERT INTO whatsapp_users" in first_call_sql
    delete_call_sql = ctx._conn.execute.call_args_list[1][0][0]
    assert "DELETE FROM wa_staging_contacts" in delete_call_sql


# ---------------------------------------------------------------------------
# 4. N events with duplicate platform_user_id — all counted in user_merged
#    (dedup happens in SQL via DISTINCT ON; Python side reports raw row count)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_batch_duplicate_platform_user_ids():
    ctx = _make_ctx()
    rows = [
        _row(id=1, platform_user_id="6591234567@s.whatsapp.net", name="Alice"),
        _row(id=2, platform_user_id="6591234567@s.whatsapp.net", name="Alice Updated"),
        _row(id=3, platform_user_id="6599999999@s.whatsapp.net", name="Bob"),
    ]
    ctx._conn.fetch = AsyncMock(return_value=rows)

    user_merged, lid_merged = await WhatsappStagingMergeHandler._merge_batch(
        ctx.pool, batch_max=1000
    )

    assert user_merged == 3  # all 3 rows passed to the INSERT (SQL handles dedup)
    assert lid_merged == 0
    # DELETE by id range: min_id=1, max_id=3
    delete_call = ctx._conn.execute.call_args_list[-1]
    assert delete_call[0][1] == 1   # min_id
    assert delete_call[0][2] == 3   # max_id


# ---------------------------------------------------------------------------
# 5. NULL name preserved via COALESCE — NULL rows included in user_rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_batch_null_name_included():
    """A row with name=None must still be included in user_rows.
    The SQL COALESCE(EXCLUDED.name, whatsapp_users.name) preserves existing.
    """
    ctx = _make_ctx()
    rows = [_row(id=1, name=None, pushname=None)]
    ctx._conn.fetch = AsyncMock(return_value=rows)

    user_merged, lid_merged = await WhatsappStagingMergeHandler._merge_batch(
        ctx.pool, batch_max=1000
    )

    assert user_merged == 1
    # The NULL name is passed through to the INSERT — COALESCE in SQL handles it
    insert_call = ctx._conn.execute.call_args_list[0]
    names_arg = insert_call[0][2]  # second positional arg after SQL = names list
    assert names_arg == [None]


# ---------------------------------------------------------------------------
# 6. is_business OR-merge: a False row followed by a True row → True in prod
#    (SQL: COALESCE(existing, FALSE) OR COALESCE(excluded, FALSE))
#    Python side: both rows included; SQL handles the merge.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_batch_is_business_rows_included():
    ctx = _make_ctx()
    rows = [
        _row(id=1, is_business=False),
        _row(id=2, platform_user_id="6591234568@s.whatsapp.net", is_business=True),
    ]
    ctx._conn.fetch = AsyncMock(return_value=rows)

    user_merged, lid_merged = await WhatsappStagingMergeHandler._merge_batch(
        ctx.pool, batch_max=1000
    )

    assert user_merged == 2
    insert_call = ctx._conn.execute.call_args_list[0]
    is_business_arg = insert_call[0][5]  # 5th positional = is_business list
    assert is_business_arg == [False, True]


# ---------------------------------------------------------------------------
# 7. lid_map skipped when lid or phone_jid absent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_batch_lid_map_skipped_when_no_lid():
    ctx = _make_ctx()
    rows = [_row(id=1, lid=None, phone_jid=None)]
    ctx._conn.fetch = AsyncMock(return_value=rows)

    user_merged, lid_merged = await WhatsappStagingMergeHandler._merge_batch(
        ctx.pool, batch_max=1000
    )

    assert lid_merged == 0
    # Only whatsapp_users INSERT + DELETE — no lid_map INSERT
    assert ctx._conn.execute.call_count == 2
    sqls = [c[0][0] for c in ctx._conn.execute.call_args_list]
    assert not any("whatsapp_lid_map" in s for s in sqls)


@pytest.mark.asyncio
async def test_merge_batch_lid_map_skipped_when_phone_jid_absent():
    ctx = _make_ctx()
    rows = [_row(id=1, lid="1234567890@lid", phone_jid=None)]
    ctx._conn.fetch = AsyncMock(return_value=rows)

    _, lid_merged = await WhatsappStagingMergeHandler._merge_batch(
        ctx.pool, batch_max=1000
    )

    assert lid_merged == 0


# ---------------------------------------------------------------------------
# 8. lid_map merged when both lid + phone_jid present
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_batch_lid_map_merged_when_both_present():
    ctx = _make_ctx()
    rows = [
        _row(
            id=1,
            platform_user_id="1234567890@lid",
            lid="1234567890@lid",
            phone_jid="6591234567@s.whatsapp.net",
        )
    ]
    ctx._conn.fetch = AsyncMock(return_value=rows)

    user_merged, lid_merged = await WhatsappStagingMergeHandler._merge_batch(
        ctx.pool, batch_max=1000
    )

    assert lid_merged == 1
    sqls = [c[0][0] for c in ctx._conn.execute.call_args_list]
    assert any("whatsapp_lid_map" in s for s in sqls)



# ===========================================================================
# Staging-write path: WhatsappCollector._stage_contacts_batch
# ===========================================================================
#
# The stager COPYs contact events into wa_staging_contacts (UNLOGGED). It is
# routed to by src.collectors.whatsapp._flusher._flush when
# WA_STAGING_ENABLED=1; otherwise the Sprint-4 _upsert_contacts_batch path
# runs. Below we exercise _stage_contacts_batch directly with a stubbed pool.


class _StagerStub:
    """Minimal shim exposing the fields _stage_contacts_batch needs."""

    def __init__(self, pool):
        self.pool = pool

    # Re-bind the real coroutine so we can call it against the stub `self`.
    from src.collectors.whatsapp import WhatsappCollector as _WC
    _stage_contacts_batch = _WC._stage_contacts_batch


def _make_pool():
    conn = MagicMock()
    conn.copy_records_to_table = AsyncMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool, conn


@pytest.mark.asyncio
async def test_stage_contacts_batch_empty_is_noop():
    pool, conn = _make_pool()
    stager = _StagerStub(pool)
    await stager._stage_contacts_batch([])
    conn.copy_records_to_table.assert_not_called()


@pytest.mark.asyncio
async def test_stage_contacts_batch_no_pool_is_noop():
    stager = _StagerStub(pool=None)
    await stager._stage_contacts_batch([{"jid": "6591234567@s.whatsapp.net"}])
    # No pool → nothing to assert beyond "did not raise".


@pytest.mark.asyncio
async def test_stage_contacts_batch_single_user_jid_copied():
    pool, conn = _make_pool()
    stager = _StagerStub(pool)
    await stager._stage_contacts_batch([
        {
            "jid": "6591234567@s.whatsapp.net",
            "pushName": "Alice",
            "display_name": "Alice",
        }
    ])
    conn.copy_records_to_table.assert_awaited_once()
    args, kwargs = conn.copy_records_to_table.call_args
    assert args[0] == "wa_staging_contacts"
    assert kwargs["columns"] == (
        "platform_user_id", "name", "pushname", "phone_number",
        "is_business", "lid", "phone_jid", "collected_at",
    )
    records = kwargs["records"]
    assert len(records) == 1
    puid, name, pushname, phone, is_biz, lid, phone_jid, ts = records[0]
    assert puid == "6591234567@s.whatsapp.net"
    assert name == "Alice"
    assert pushname == "Alice"
    assert phone == "6591234567"   # derived from JID prefix
    assert lid is None
    assert phone_jid == "6591234567@s.whatsapp.net"


@pytest.mark.asyncio
async def test_stage_contacts_batch_duplicates_preserved():
    # Staging is append-only. Dedup happens at merge time (DISTINCT ON in
    # wa_staging_merge). The stager must copy every valid event exactly once.
    pool, conn = _make_pool()
    stager = _StagerStub(pool)
    await stager._stage_contacts_batch([
        {"jid": "6591234567@s.whatsapp.net", "pushName": "Alice"},
        {"jid": "6591234567@s.whatsapp.net", "pushName": "Alice Updated"},
        {"jid": "6599999999@s.whatsapp.net", "pushName": "Bob"},
    ])
    records = conn.copy_records_to_table.call_args.kwargs["records"]
    assert len(records) == 3


@pytest.mark.asyncio
async def test_stage_contacts_batch_lid_mapping_row_copied():
    pool, conn = _make_pool()
    stager = _StagerStub(pool)
    await stager._stage_contacts_batch([
        {
            "lid": "1234567890@lid",
            "jid": "6591234567@s.whatsapp.net",
            "pushName": "Alice",
        }
    ])
    records = conn.copy_records_to_table.call_args.kwargs["records"]
    assert len(records) == 1
    _, _, _, _, _, lid, phone_jid, _ = records[0]
    assert lid == "1234567890@lid"
    assert phone_jid == "6591234567@s.whatsapp.net"


@pytest.mark.asyncio
async def test_stage_contacts_batch_skips_events_without_platform_id():
    pool, conn = _make_pool()
    stager = _StagerStub(pool)
    await stager._stage_contacts_batch([
        {"pushName": "no-jid, no-lid"},                       # skipped
        {"platform_user_id": "6591234567@s.whatsapp.net"},    # kept
    ])
    records = conn.copy_records_to_table.call_args.kwargs["records"]
    assert len(records) == 1
    assert records[0][0] == "6591234567@s.whatsapp.net"


@pytest.mark.asyncio
async def test_stage_contacts_batch_skips_non_user_jid_non_lid():
    pool, conn = _make_pool()
    stager = _StagerStub(pool)
    await stager._stage_contacts_batch([
        {"platform_user_id": "group-thing@g.us"},   # not user jid, no lid mapping → skipped
    ])
    conn.copy_records_to_table.assert_not_called()


def test_wa_staging_enabled_helper(monkeypatch):
    from src.collectors.whatsapp import _wa_staging_enabled
    monkeypatch.setenv("WA_STAGING_ENABLED", "1")
    assert _wa_staging_enabled() is True
    monkeypatch.setenv("WA_STAGING_ENABLED", "0")
    assert _wa_staging_enabled() is False
    monkeypatch.delenv("WA_STAGING_ENABLED", raising=False)
    assert _wa_staging_enabled() is False
