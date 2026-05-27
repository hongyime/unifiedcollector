"""Tests for src/collectors/matrix.py — Wave 1 Phase 0 orchestrator.

Pure-unit. matrix-nio is never imported live; we drive the collector via
a stubbed BeeperMatrixClient. No network, no docker, no real db.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors import matrix as matrix_mod
from src.collectors.matrix import MatrixCollector, is_enabled
from src.core.matrix_client import (
    MatrixClientError,
    MatrixLoginError,
    RoomSummary,
)


# ── helpers ───────────────────────────────────────────────────────────────


class StubSyncResponse:
    def __init__(self, next_batch: str = "tok-NEW") -> None:
        self.next_batch = next_batch


def _make_stub_client(
    *,
    rooms: dict[str, RoomSummary] | None = None,
    sync_returns: Any = None,
    sync_raises: BaseException | None = None,
    user_id: str = "@u:beeper.com",
):
    """Build a MagicMock-backed stand-in for BeeperMatrixClient.

    We don't subclass the real thing because the real `sync_once` is wrapped
    in @async_retry and would multiply calls under failure paths; an
    AsyncMock gives us deterministic per-call control for warmup tests.
    """
    client = MagicMock()
    client.user_id = user_id
    client.list_rooms = AsyncMock(return_value=rooms or {})

    if sync_raises is not None:
        client.sync_once = AsyncMock(side_effect=sync_raises)
    else:
        client.sync_once = AsyncMock(
            return_value=sync_returns or StubSyncResponse()
        )

    # _sync_state.save() — collect() calls this directly.
    client._sync_state = MagicMock()
    client._sync_state.save = AsyncMock()

    return client


# ── feature gate ──────────────────────────────────────────────────────────


def test_collector_disabled_via_env(monkeypatch):
    """Default (env unset) → disabled. Various truthy/falsy values."""
    monkeypatch.delenv("MATRIX_COLLECTOR_ENABLED", raising=False)
    assert is_enabled() is False

    for falsy in ("", "0", "false", "no", "off", "  ", "FALSE"):
        monkeypatch.setenv("MATRIX_COLLECTOR_ENABLED", falsy)
        assert is_enabled() is False, f"expected falsy for {falsy!r}"

    for truthy in ("1", "true", "yes", "on", "TRUE", " on "):
        monkeypatch.setenv("MATRIX_COLLECTOR_ENABLED", truthy)
        assert is_enabled() is True, f"expected truthy for {truthy!r}"


def test_constructor_requires_client():
    with pytest.raises(ValueError):
        MatrixCollector(client=None)


# ── discover_rooms ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_rooms_summary_format(caplog):
    rooms = {
        "!a:b": RoomSummary(
            room_id="!a:b",
            display_name="Alpha Room",
            topic="t",
            member_count=4,
            encrypted=False,
            last_activity_ts=1_700_000_000_000,
        ),
        "!c:d": RoomSummary(
            room_id="!c:d",
            display_name="Bravo Encrypted",
            topic=None,
            member_count=12,
            encrypted=True,
            last_activity_ts=1_700_000_500_000,
        ),
        "!e:f": RoomSummary(
            room_id="!e:f",
            display_name=None,
            topic=None,
            member_count=2,
            encrypted=False,
            last_activity_ts=None,
        ),
    }
    client = _make_stub_client(rooms=rooms)
    coll = MatrixCollector(client=client)

    with caplog.at_level("INFO", logger="src.collectors.matrix"):
        out = await coll.discover_rooms()

    assert out == rooms
    client.list_rooms.assert_awaited_once()

    text = "\n".join(rec.getMessage() for rec in caplog.records)
    # Header line with count
    assert "discovered 3 room(s)" in text
    # Each room id surfaces in the per-room log line
    assert "!a:b" in text and "!c:d" in text and "!e:f" in text
    # E2EE flag formatted distinctly
    assert "E2EE" in text and "plain" in text
    # Member count printed
    assert "members=4" in text and "members=12" in text
    # Missing display name does not crash; falls back
    assert "(no name)" in text


# ── collect ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_persists_next_batch_to_db():
    client = _make_stub_client(sync_returns=StubSyncResponse(next_batch="next-XYZ"))
    coll = MatrixCollector(client=client)

    out = await coll.collect()

    assert out == "next-XYZ"
    client.sync_once.assert_awaited_once()
    # The collector explicitly re-saves to give tests a single seam,
    # which is the contract we documented.
    client._sync_state.save.assert_awaited_once_with("next-XYZ")


@pytest.mark.asyncio
async def test_collect_handles_missing_next_batch(caplog):
    """If sync returns no next_batch token, collect() warns and returns None
    rather than persisting an empty cursor."""
    resp = MagicMock(spec=[])  # no next_batch attr
    client = _make_stub_client(sync_returns=resp)
    coll = MatrixCollector(client=client)

    with caplog.at_level("WARNING", logger="src.collectors.matrix"):
        out = await coll.collect()

    assert out is None
    client._sync_state.save.assert_not_awaited()
    assert any("no next_batch" in r.getMessage() for r in caplog.records)


# ── warmup ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_warmup_success():
    client = _make_stub_client()
    coll = MatrixCollector(client=client)
    assert await coll.warmup() is True
    client.sync_once.assert_awaited_once()
    # Must use the short timeout — that's the contract: we don't want a
    # warmup probe to hang for 30s on a dead homeserver.
    kwargs = client.sync_once.await_args.kwargs
    assert kwargs.get("timeout_ms") == 5_000


@pytest.mark.asyncio
async def test_warmup_handles_unauthorized(caplog):
    client = _make_stub_client(
        sync_raises=MatrixLoginError("M_UNKNOWN_TOKEN: token rejected"),
    )
    coll = MatrixCollector(client=client)

    with caplog.at_level("ERROR", logger="src.collectors.matrix"):
        ok = await coll.warmup()

    assert ok is False
    assert any("token rejected" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_warmup_handles_homeserver_unreachable(caplog):
    client = _make_stub_client(
        sync_raises=MatrixClientError("connection refused"),
    )
    coll = MatrixCollector(client=client)

    with caplog.at_level("ERROR", logger="src.collectors.matrix"):
        ok = await coll.warmup()

    assert ok is False
    assert any(
        "homeserver unreachable" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_warmup_swallows_unexpected_exceptions(caplog):
    """warmup() must NEVER raise — schedulers depend on the bool return."""
    client = _make_stub_client(sync_raises=RuntimeError("totally unexpected"))
    coll = MatrixCollector(client=client)

    with caplog.at_level("ERROR", logger="src.collectors.matrix"):
        ok = await coll.warmup()

    assert ok is False


# ── module-level smoke ────────────────────────────────────────────────────


def test_module_exports():
    assert "MatrixCollector" in matrix_mod.__all__
    assert "is_enabled" in matrix_mod.__all__
