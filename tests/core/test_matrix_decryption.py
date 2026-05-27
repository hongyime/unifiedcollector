"""Unit tests for src/core/matrix_decryption.py.

Pure-unit. matrix-nio's AsyncClient is replaced with a plain Mock — the
only contract we depend on is ``client._client.decrypt_event`` and
``client._client.restore_room_keys_from_backup``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from src.core.matrix_decryption import (
    KeyBackupRestoreError,
    MatrixDecryptionError,
    MatrixDecryptionService,
)


# ── helpers ───────────────────────────────────────────────────────────────


def _make_client(
    *,
    decrypt_return: Any = None,
    decrypt_raises: BaseException | None = None,
    restore: Any = None,
    restore_missing: bool = False,
):
    """Build a fake `BeeperMatrixClient`-shaped object."""
    nio_client = MagicMock()
    if decrypt_raises is not None:
        nio_client.decrypt_event = AsyncMock(side_effect=decrypt_raises)
    else:
        nio_client.decrypt_event = AsyncMock(return_value=decrypt_return)
    if restore_missing:
        # Force absence — del the auto-Mock attribute.
        if hasattr(nio_client, "restore_room_keys_from_backup"):
            del nio_client.restore_room_keys_from_backup
    else:
        nio_client.restore_room_keys_from_backup = AsyncMock(return_value=restore)

    outer = MagicMock()
    outer._client = nio_client
    outer.user_id = "@u:beeper.com"
    return outer, nio_client


def _make_writer(undecrypted_rows=None):
    rows = undecrypted_rows or []
    writer = MagicMock()
    writer.get_undecrypted_events = AsyncMock(return_value=rows)
    writer.update_decrypted = AsyncMock()
    return writer


# ── construction ──────────────────────────────────────────────────────────


def test_constructor_requires_client():
    with pytest.raises(ValueError):
        MatrixDecryptionService(client=None)


def test_constructor_default_breaker():
    client, _ = _make_client()
    svc = MatrixDecryptionService(client)
    snap = svc.breaker_state()
    assert snap["failure_threshold"] == 20
    assert snap["recovery_timeout"] == 300.0


# ── restore_keys_from_backup ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_returns_false_without_recovery_key(monkeypatch):
    monkeypatch.delenv("BEEPER_KEY_BACKUP_RECOVERY_KEY", raising=False)
    client, _ = _make_client()
    svc = MatrixDecryptionService(client)
    assert await svc.restore_keys_from_backup() is False


@pytest.mark.asyncio
async def test_restore_uses_env_var(monkeypatch):
    monkeypatch.setenv("BEEPER_KEY_BACKUP_RECOVERY_KEY", "EsTk-xxxx")
    client, nio_client = _make_client(restore="ok")
    svc = MatrixDecryptionService(client)
    assert await svc.restore_keys_from_backup() is True
    nio_client.restore_room_keys_from_backup.assert_awaited_once_with("EsTk-xxxx")


@pytest.mark.asyncio
async def test_restore_idempotent(monkeypatch):
    monkeypatch.setenv("BEEPER_KEY_BACKUP_RECOVERY_KEY", "K")
    client, nio_client = _make_client(restore="ok")
    svc = MatrixDecryptionService(client)
    await svc.restore_keys_from_backup()
    await svc.restore_keys_from_backup()
    # Only ONE network-side call regardless of how many times caller invokes.
    assert nio_client.restore_room_keys_from_backup.await_count == 1


@pytest.mark.asyncio
async def test_restore_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv("BEEPER_KEY_BACKUP_RECOVERY_KEY", "ENV-KEY")
    client, nio_client = _make_client(restore="ok")
    svc = MatrixDecryptionService(client)
    await svc.restore_keys_from_backup(recovery_key="EXPLICIT")
    nio_client.restore_room_keys_from_backup.assert_awaited_once_with("EXPLICIT")


@pytest.mark.asyncio
async def test_restore_no_logged_in_client():
    client = MagicMock()
    client._client = None
    client.user_id = "@u:b"
    svc = MatrixDecryptionService(client)
    with pytest.raises(KeyBackupRestoreError):
        await svc.restore_keys_from_backup(recovery_key="K")


@pytest.mark.asyncio
async def test_restore_missing_nio_method_raises():
    client, _ = _make_client(restore_missing=True)
    svc = MatrixDecryptionService(client)
    with pytest.raises(KeyBackupRestoreError):
        await svc.restore_keys_from_backup(recovery_key="K")


@pytest.mark.asyncio
async def test_restore_underlying_failure_wrapped():
    client, nio_client = _make_client()
    nio_client.restore_room_keys_from_backup = AsyncMock(
        side_effect=RuntimeError("bad recovery key")
    )
    svc = MatrixDecryptionService(client)
    with pytest.raises(KeyBackupRestoreError):
        await svc.restore_keys_from_backup(recovery_key="K")


# ── decrypt_event ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decrypt_event_returns_dict_path():
    client, _ = _make_client(
        decrypt_return={
            "type": "m.room.message",
            "content": {"body": "hello world", "msgtype": "m.text"},
        }
    )
    svc = MatrixDecryptionService(client)
    body, decrypted = await svc.decrypt_event("$1", {"foo": "bar"})
    assert body == "hello world"
    assert decrypted["content"]["body"] == "hello world"


@pytest.mark.asyncio
async def test_decrypt_event_returns_object_with_source():
    nio_event = MagicMock()
    nio_event.body = "obj-body"
    nio_event.source = {"type": "m.room.message", "content": {"body": "obj-body"}}
    client, _ = _make_client(decrypt_return=nio_event)
    svc = MatrixDecryptionService(client)
    body, decrypted = await svc.decrypt_event("$1", {})
    assert body == "obj-body"
    assert decrypted["content"]["body"] == "obj-body"


@pytest.mark.asyncio
async def test_decrypt_event_underlying_raises_wrapped():
    client, _ = _make_client(decrypt_raises=RuntimeError("no keys"))
    svc = MatrixDecryptionService(client)
    with pytest.raises(MatrixDecryptionError):
        await svc.decrypt_event("$1", {})


@pytest.mark.asyncio
async def test_decrypt_event_missing_client():
    client = MagicMock()
    client._client = None
    svc = MatrixDecryptionService(client)
    with pytest.raises(MatrixDecryptionError):
        await svc.decrypt_event("$1", {})


@pytest.mark.asyncio
async def test_decrypt_event_returns_non_dict_raises():
    """If the result has neither dict semantics nor .source, raise."""
    bad = MagicMock(spec=[])  # no attrs
    client, _ = _make_client(decrypt_return=bad)
    svc = MatrixDecryptionService(client)
    with pytest.raises(MatrixDecryptionError):
        await svc.decrypt_event("$1", {})


# ── decrypt_pending ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decrypt_pending_no_writer_returns_zero():
    client, _ = _make_client()
    svc = MatrixDecryptionService(client)
    out = await svc.decrypt_pending(limit=10)
    assert out == {"attempted": 0, "decrypted": 0, "failed": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_decrypt_pending_writer_missing_methods_logs():
    client, _ = _make_client()
    bad_writer = MagicMock(spec=[])
    svc = MatrixDecryptionService(client, writer=bad_writer)
    out = await svc.decrypt_pending(limit=10)
    assert out["attempted"] == 0


@pytest.mark.asyncio
async def test_decrypt_pending_decrypts_and_updates():
    client, _ = _make_client(
        decrypt_return={"type": "m.room.message", "content": {"body": "hi"}},
    )
    rows = [
        {"event_id": "$1", "room_id": "!r:s", "raw_content": {"x": 1}},
        {"event_id": "$2", "room_id": "!r:s", "raw_content": {"x": 2}},
    ]
    writer = _make_writer(undecrypted_rows=rows)
    svc = MatrixDecryptionService(client, writer=writer)
    out = await svc.decrypt_pending(limit=10)
    assert out["attempted"] == 2
    assert out["decrypted"] == 2
    assert writer.update_decrypted.await_count == 2


@pytest.mark.asyncio
async def test_decrypt_pending_failure_continues():
    """A single bad row must not abort the batch."""
    nio_event_ok = {"type": "m.room.message", "content": {"body": "ok"}}
    decrypt = AsyncMock()
    decrypt.side_effect = [
        nio_event_ok,
        RuntimeError("missing key"),
        nio_event_ok,
    ]
    client = MagicMock()
    client._client = MagicMock()
    client._client.decrypt_event = decrypt
    client.user_id = "@u:b"

    rows = [
        {"event_id": f"$e{i}", "room_id": "!r:s", "raw_content": {}}
        for i in range(3)
    ]
    writer = _make_writer(undecrypted_rows=rows)
    svc = MatrixDecryptionService(client, writer=writer)
    out = await svc.decrypt_pending()
    assert out["attempted"] == 3
    assert out["decrypted"] == 2
    assert out["failed"] == 1


# ── circuit breaker behaviour ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold():
    """20 consecutive MatrixDecryptionErrors -> breaker OPEN; 21st short-circuits."""
    client, _ = _make_client(decrypt_raises=RuntimeError("no keys"))
    svc = MatrixDecryptionService(client)

    # Drive 20 individual decrypt_event calls through the breaker.
    for i in range(20):
        with pytest.raises(MatrixDecryptionError):
            await svc._breaker.call(lambda: svc.decrypt_event(f"$e{i}", {}))

    assert svc.breaker_state()["state"] == CircuitBreaker.OPEN

    # 21st call short-circuits with CircuitOpenError BEFORE invoking decrypt.
    pre = client._client.decrypt_event.await_count
    with pytest.raises(CircuitOpenError):
        await svc._breaker.call(lambda: svc.decrypt_event("$e21", {}))
    assert client._client.decrypt_event.await_count == pre  # not called


@pytest.mark.asyncio
async def test_decrypt_pending_skips_remaining_when_breaker_opens():
    """Once the breaker trips mid-batch, the rest of the batch is skipped."""
    # Prime breaker to the OPEN state by injecting one with threshold=1.
    breaker = CircuitBreaker(
        name="t",
        failure_threshold=1,
        recovery_timeout=300.0,
        expected_exception=(MatrixDecryptionError,),
    )
    client, _ = _make_client(decrypt_raises=RuntimeError("no keys"))
    rows = [
        {"event_id": f"$e{i}", "room_id": "!r:s", "raw_content": {}}
        for i in range(5)
    ]
    writer = _make_writer(undecrypted_rows=rows)
    svc = MatrixDecryptionService(client, writer=writer, breaker=breaker)

    out = await svc.decrypt_pending()
    # First row fails, breaker trips, rest skipped.
    assert out["failed"] == 1
    assert out["skipped"] >= 4
    assert out["decrypted"] == 0


# ── module-level smoke ────────────────────────────────────────────────────


def test_module_exports():
    from src.core import matrix_decryption as m
    assert "MatrixDecryptionService" in m.__all__
    assert "MatrixDecryptionError" in m.__all__
    assert "KeyBackupRestoreError" in m.__all__
