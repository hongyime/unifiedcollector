"""Tests for src/core/matrix_client.py — read-only Beeper Matrix client.

Pure-unit: the matrix-nio AsyncClient is replaced with a stub. No live
homeserver login. Live smoke is gated on BEEPER_MATRIX_LIVE=1 and skipped
by default (would need real Beeper creds in env).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.core import matrix_client as mc
from src.core.matrix_client import (
    BeeperMatrixClient,
    MatrixClientError,
    MatrixLoginError,
    MatrixSyncFailedError,
    MatrixSyncStateRepository,
    RoomSummary,
)


# ── stub AsyncClient ──────────────────────────────────────────────────────


class StubSyncResponse:
    def __init__(self, next_batch: str = "tok-1") -> None:
        self.next_batch = next_batch


class StubLoginResponse:
    def __init__(self, access_token: str = "tk", device_id: str = "DVID") -> None:
        self.access_token = access_token
        self.device_id = device_id


class StubLoginError(Exception):
    def __init__(self, msg: str = "bad creds") -> None:
        super().__init__(msg)
        self.message = msg


class StubRoom:
    def __init__(
        self,
        room_id: str = "!a:b",
        display_name: str | None = "Room",
        topic: str | None = "T",
        member_count: int = 3,
        encrypted: bool = False,
        last_event_timestamp: int | None = 1_700_000_000_000,
    ) -> None:
        self.room_id = room_id
        self.display_name = display_name
        self.topic = topic
        self.member_count = member_count
        self.encrypted = encrypted
        self.last_event_timestamp = last_event_timestamp


class StubRoomMessagesResponse:
    def __init__(self, chunk: list[Any] | None = None, end: str = "end-tok") -> None:
        self.chunk = chunk or []
        self.end = end
        self.start = ""


class StubAsyncClient:
    """Minimal AsyncClient surface used by BeeperMatrixClient."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.access_token: str | None = None
        self.device_id: str | None = kwargs.get("device_id")
        self.user_id: str = kwargs.get("user", "")
        self.rooms: dict[str, StubRoom] = {}
        self.login = AsyncMock()
        self.sync = AsyncMock(return_value=StubSyncResponse())
        self.room_messages = AsyncMock(return_value=StubRoomMessagesResponse())
        self.close = AsyncMock()


@pytest.fixture(autouse=True)
def patch_nio_classes(monkeypatch):
    """Force module-level type checks (isinstance(SyncResponse), etc.) to
    compare against our stub classes so the production isinstance() guards
    don't reject our stub responses.
    """
    monkeypatch.setattr(mc, "SyncResponse", StubSyncResponse)
    monkeypatch.setattr(mc, "RoomMessagesResponse", StubRoomMessagesResponse)
    monkeypatch.setattr(mc, "LoginResponse", StubLoginResponse)
    monkeypatch.setattr(mc, "LoginError", StubLoginError)
    # MegolmEvent stays as imported from real nio for is_undecryptable test.


def _make_client(**overrides) -> BeeperMatrixClient:
    """Build a BeeperMatrixClient wired to the stub factory."""
    stub = StubAsyncClient(
        homeserver=overrides.get("homeserver", "https://matrix.beeper.com"),
        user=overrides.get("user_id", "@u:beeper.com"),
        device_id=overrides.get("device_id"),
        store_path=overrides.get("store_path"),
    )
    overrides.setdefault("_stub", stub)

    def factory(**kwargs):
        return overrides["_stub"]

    return BeeperMatrixClient(
        homeserver=overrides.get("homeserver", "https://matrix.beeper.com"),
        user_id=overrides.get("user_id", "@u:beeper.com"),
        store_path=overrides.get("store_path"),
        pool=overrides.get("pool"),
        device_id=overrides.get("device_id"),
        client_factory=factory,
    )


# ── construction ──────────────────────────────────────────────────────────


def test_construct_requires_homeserver():
    with pytest.raises(ValueError):
        BeeperMatrixClient(homeserver="", user_id="@u:b")


def test_construct_requires_user():
    with pytest.raises(ValueError):
        BeeperMatrixClient(homeserver="https://h", user_id="")


def test_construct_creates_store_path(tmp_path):
    p = tmp_path / "matrix_store"
    BeeperMatrixClient(
        homeserver="https://h",
        user_id="@u:b",
        store_path=str(p),
        client_factory=lambda **kw: StubAsyncClient(**kw),
    )
    assert p.exists() and p.is_dir()


# ── login ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_password_success():
    stub = StubAsyncClient()
    # nio's login mutates the client; emulate that.
    async def fake_login(password, device_name=None):
        stub.access_token = "tk"
        stub.device_id = "DVID"
        return StubLoginResponse()
    stub.login = fake_login

    c = BeeperMatrixClient(
        homeserver="https://h", user_id="@u:b",
        client_factory=lambda **kw: stub,
    )
    await c.login(password="pw")
    assert c.access_token == "tk"
    assert c.device_id == "DVID"


@pytest.mark.asyncio
async def test_login_password_rejected():
    stub = StubAsyncClient()

    async def fake_login(password, device_name=None):
        return StubLoginError("nope")
    stub.login = fake_login

    c = BeeperMatrixClient(
        homeserver="https://h", user_id="@u:b",
        client_factory=lambda **kw: stub,
    )
    with pytest.raises(MatrixLoginError):
        await c.login(password="bad")


@pytest.mark.asyncio
async def test_login_no_credentials_raises():
    c = _make_client()
    with pytest.raises(ValueError):
        await c.login()


@pytest.mark.asyncio
async def test_login_with_access_token_skips_network():
    stub = StubAsyncClient()
    stub.login = AsyncMock(side_effect=AssertionError("should not be called"))
    c = BeeperMatrixClient(
        homeserver="https://h",
        user_id="@u:b",
        device_id="DV",
        client_factory=lambda **kw: stub,
    )
    await c.login(access_token="cached-token")
    assert c.access_token == "cached-token"
    stub.login.assert_not_called()


@pytest.mark.asyncio
async def test_login_password_returns_no_token_raises():
    stub = StubAsyncClient()

    async def fake_login(password, device_name=None):
        # leave access_token None
        return StubLoginResponse(access_token="")
    stub.login = fake_login
    # Have to also leave stub.access_token unset.
    stub.access_token = None

    c = BeeperMatrixClient(
        homeserver="https://h", user_id="@u:b",
        client_factory=lambda **kw: stub,
    )
    with pytest.raises(MatrixLoginError):
        await c.login(password="pw")


# ── sync_once ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_once_requires_login():
    c = _make_client()
    with pytest.raises(MatrixClientError):
        await c.sync_once()


@pytest.mark.asyncio
async def test_sync_once_persists_next_batch():
    stub = StubAsyncClient()
    stub.access_token = "tk"
    stub.sync = AsyncMock(return_value=StubSyncResponse(next_batch="batch-42"))
    c = BeeperMatrixClient(
        homeserver="https://h", user_id="@u:b",
        client_factory=lambda **kw: stub,
    )
    c._client = stub  # simulate post-login

    saved = {}

    async def fake_save(token):
        saved["t"] = token
    c._sync_state.save = fake_save  # type: ignore
    c._sync_state.load = AsyncMock(return_value=None)  # type: ignore

    resp = await c.sync_once(timeout_ms=1000)
    assert resp.next_batch == "batch-42"
    assert saved["t"] == "batch-42"


@pytest.mark.asyncio
async def test_sync_once_resumes_with_persisted_token():
    stub = StubAsyncClient()
    stub.access_token = "tk"
    stub.sync = AsyncMock(return_value=StubSyncResponse(next_batch="b2"))
    c = BeeperMatrixClient(
        homeserver="https://h", user_id="@u:b",
        client_factory=lambda **kw: stub,
    )
    c._client = stub
    c._sync_state.load = AsyncMock(return_value="prior-token")  # type: ignore
    c._sync_state.save = AsyncMock()  # type: ignore

    await c.sync_once()
    # The sync call should have received `since=prior-token`.
    args, kwargs = stub.sync.call_args
    assert kwargs.get("since") == "prior-token"


@pytest.mark.asyncio
async def test_sync_once_non_sync_response_raises():
    stub = StubAsyncClient()
    stub.access_token = "tk"

    class NotASyncResp:
        message = "boom"

    # async_retry will retry 3x on the inner exception, then re-raise.
    stub.sync = AsyncMock(return_value=NotASyncResp())
    c = BeeperMatrixClient(
        homeserver="https://h", user_id="@u:b",
        client_factory=lambda **kw: stub,
    )
    c._client = stub
    c._sync_state.load = AsyncMock(return_value=None)  # type: ignore
    c._sync_state.save = AsyncMock()  # type: ignore

    with pytest.raises(MatrixClientError):
        await c.sync_once()


# ── sync_forever ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_forever_invokes_callback_then_stops():
    stub = StubAsyncClient()
    stub.access_token = "tk"
    stub.sync = AsyncMock(return_value=StubSyncResponse(next_batch="b"))
    c = BeeperMatrixClient(
        homeserver="https://h", user_id="@u:b",
        client_factory=lambda **kw: stub,
    )
    c._client = stub
    c._sync_state.load = AsyncMock(return_value=None)  # type: ignore
    c._sync_state.save = AsyncMock()  # type: ignore

    seen = []

    async def cb(resp):
        seen.append(resp)
        if len(seen) >= 2:
            c.stop()

    await c.sync_forever(cb, timeout_ms=10)
    assert len(seen) >= 2


@pytest.mark.asyncio
async def test_sync_forever_raises_after_consecutive_failures(monkeypatch):
    """5 sync_once failures in a row → MatrixSyncFailedError."""
    stub = StubAsyncClient()
    stub.access_token = "tk"
    c = BeeperMatrixClient(
        homeserver="https://h", user_id="@u:b",
        client_factory=lambda **kw: stub,
    )
    c._client = stub
    c._sync_state.load = AsyncMock(return_value=None)  # type: ignore
    c._sync_state.save = AsyncMock()  # type: ignore

    # Make sync_once unconditionally raise — bypass async_retry's own retries
    # by patching the bound method on the instance.
    async def boom(timeout_ms=30000):
        raise RuntimeError("sync exploded")

    monkeypatch.setattr(c, "sync_once", boom)
    # Make backoff sleep instant.
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    with pytest.raises(MatrixSyncFailedError):
        await c.sync_forever(AsyncMock(), timeout_ms=1)


# ── list_rooms ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_rooms_returns_summaries():
    stub = StubAsyncClient()
    stub.access_token = "tk"
    stub.rooms = {
        "!r1:b": StubRoom(room_id="!r1:b", display_name="One", member_count=5),
        "!r2:b": StubRoom(room_id="!r2:b", display_name="Two", encrypted=True),
    }
    c = BeeperMatrixClient(
        homeserver="https://h", user_id="@u:b",
        client_factory=lambda **kw: stub,
    )
    c._client = stub
    rooms = await c.list_rooms()
    assert set(rooms.keys()) == {"!r1:b", "!r2:b"}
    assert isinstance(rooms["!r1:b"], RoomSummary)
    assert rooms["!r1:b"].member_count == 5
    assert rooms["!r2:b"].encrypted is True
    assert rooms["!r1:b"].last_activity_ts == 1_700_000_000_000


@pytest.mark.asyncio
async def test_list_rooms_empty_when_no_rooms():
    stub = StubAsyncClient()
    stub.access_token = "tk"
    stub.rooms = {}
    c = BeeperMatrixClient(
        homeserver="https://h", user_id="@u:b",
        client_factory=lambda **kw: stub,
    )
    c._client = stub
    assert await c.list_rooms() == {}


# ── fetch_history ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_history_calls_room_messages_back_direction():
    stub = StubAsyncClient()
    stub.access_token = "tk"
    stub.room_messages = AsyncMock(
        return_value=StubRoomMessagesResponse(chunk=["e1", "e2"], end="next-tok")
    )
    c = BeeperMatrixClient(
        homeserver="https://h", user_id="@u:b",
        client_factory=lambda **kw: stub,
    )
    c._client = stub

    resp = await c.fetch_history("!r1:b", limit=50, before_token="tok-x")
    assert resp.chunk == ["e1", "e2"]
    args, kwargs = stub.room_messages.call_args
    assert kwargs["room_id"] == "!r1:b"
    assert kwargs["limit"] == 50
    assert kwargs["start"] == "tok-x"
    assert kwargs["direction"] == "b"


@pytest.mark.asyncio
async def test_fetch_history_rejects_zero_limit():
    c = _make_client()
    c._client = StubAsyncClient()
    c._client.access_token = "tk"
    with pytest.raises(ValueError):
        await c.fetch_history("!r:b", limit=0)


# ── encryption ────────────────────────────────────────────────────────────


def test_is_undecryptable_with_megolm_event():
    # Real nio class — instantiate a minimal MegolmEvent if available.
    from nio import MegolmEvent  # noqa: WPS433

    # MegolmEvent has many required fields; build minimally via __new__ to
    # sidestep its constructor.
    ev = MegolmEvent.__new__(MegolmEvent)
    assert BeeperMatrixClient.is_undecryptable(ev) is True


def test_is_undecryptable_false_for_other():
    assert BeeperMatrixClient.is_undecryptable("not an event") is False
    assert BeeperMatrixClient.is_undecryptable(None) is False


# ── repository (no DB; pool=None path) ────────────────────────────────────


@pytest.mark.asyncio
async def test_repo_load_returns_none_without_pool():
    repo = MatrixSyncStateRepository(pool=None, user_id="@u:b")
    assert await repo.load() is None


@pytest.mark.asyncio
async def test_repo_save_no_op_without_pool():
    repo = MatrixSyncStateRepository(pool=None, user_id="@u:b")
    await repo.save("anything")  # must not raise


# ── close / lifecycle ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_invokes_underlying_client_close():
    stub = StubAsyncClient()
    c = BeeperMatrixClient(
        homeserver="https://h", user_id="@u:b",
        client_factory=lambda **kw: stub,
    )
    c._client = stub
    await c.close()
    stub.close.assert_called_once()


# ── live smoke (gated) ────────────────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("BEEPER_MATRIX_LIVE") != "1",
    reason="live Beeper test gated on BEEPER_MATRIX_LIVE=1",
)
@pytest.mark.asyncio
async def test_live_login_and_one_sync():  # pragma: no cover - live only
    homeserver = os.environ["BEEPER_HOMESERVER"]
    user = os.environ["BEEPER_MATRIX_USER"]
    pw = os.environ["BEEPER_MATRIX_PASSWORD"]
    c = BeeperMatrixClient(homeserver=homeserver, user_id=user)
    await c.login(password=pw)
    resp = await c.sync_once(timeout_ms=5000)
    assert resp is not None
    await c.close()
