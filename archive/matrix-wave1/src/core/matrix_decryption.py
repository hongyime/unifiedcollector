"""Wave 1 Phase 1: Matrix E2EE decryption service.

Owns the post-sync responsibility of turning encrypted matrix events that
were ingested by the writer (with `is_encrypted=TRUE` and
`is_decrypted=FALSE`) into plaintext and updating their rows.

Two responsibilities:

1. ``restore_keys_from_backup(recovery_key)`` — runs ONCE on collector
   boot when the operator has populated ``BEEPER_KEY_BACKUP_RECOVERY_KEY``.
   Pulls megolm session keys from the homeserver's Online Key Backup so
   the local olm/megolm store can decrypt historical messages. We never
   *write* keys back to the backup — strictly RESTORE.

2. ``decrypt_pending(limit)`` — every collect() cycle, fetch undecrypted
   rows via ``writer.get_undecrypted_events`` and try to decrypt each via
   the underlying ``client.decrypt_event`` method. Successful decrypts
   are persisted via ``writer.update_decrypted``; failures are logged
   and skipped (the row stays ``is_decrypted=FALSE`` and will be retried
   next cycle — the keys may simply not have arrived yet).

A single ``CircuitBreaker`` (failure_threshold=20, recovery_timeout=300)
wraps the per-event decrypt call so a wholesale failure (e.g. the
recovery key is wrong, libolm is broken) doesn't loop forever.

This module deliberately does NOT call out to httpx, the database
directly, or any other side-effect — both collaborators (`client` and
`writer`) are injected so unit tests can drive it with mocks.

Phase 1 ships behind ``MATRIX_COLLECTOR_ENABLED``; without Beeper creds
the collector never instantiates this service and these calls never run.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Optional

from src.core.circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)


# ── public exceptions ─────────────────────────────────────────────────────


class MatrixDecryptionError(Exception):
    """A specific event could not be decrypted in this cycle.

    Distinct from ``src.core.matrix_client.MatrixDecryptionError`` (which
    refers to the sync-time MegolmEvent surfacing): this one is the
    decrypt-from-stored-row path.
    """


class KeyBackupRestoreError(Exception):
    """restore_keys_from_backup failed (bad recovery key, network, …)."""


# ── service ───────────────────────────────────────────────────────────────


class MatrixDecryptionService:
    """Decrypts encrypted rows produced by the matrix event writer.

    Constructor parameters
    ──────────────────────
    client : object
        A ``BeeperMatrixClient`` (or, in tests, a stub exposing the same
        attributes). Specifically we expect:
            * ``client._client`` — the matrix-nio AsyncClient (has
              ``decrypt_event`` and, in some nio versions,
              ``restore_room_keys_from_backup`` / ``import_room_keys``).
            * ``client.user_id`` for logging.
    writer : object
        Anything quacking like the DB-side ``MatrixEventWriter`` from the
        sister Phase 1 patch:
            * ``async get_undecrypted_events(limit) -> list[dict]``
              (rows must include ``event_id``, ``room_id``, ``raw_content``)
            * ``async update_decrypted(event_id, plaintext_body,
              decrypted_content) -> None``
        If ``writer is None`` we operate in "decrypt-only" mode — useful
        for tests and for the moment when the DB agent's writer hasn't
        landed yet; ``decrypt_pending`` becomes a no-op.
    breaker : CircuitBreaker | None
        Optional injection for tests. Default: 20 failures / 300s.
    """

    def __init__(
        self,
        client: Any,
        writer: Any = None,
        *,
        breaker: Optional[CircuitBreaker] = None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        if client is None:
            raise ValueError("client is required")
        self.client = client
        self.writer = writer
        self.log = log or logger
        self._breaker = breaker or CircuitBreaker(
            name=f"matrix-decrypt:{getattr(client, 'user_id', '?')}",
            failure_threshold=20,
            recovery_timeout=300.0,
            # Never count CircuitOpenError as a failure (would compound).
            expected_exception=(MatrixDecryptionError,),
        )
        self._keys_restored: bool = False

    # ── key backup ────────────────────────────────────────────────────

    async def restore_keys_from_backup(
        self,
        recovery_key: Optional[str] = None,
    ) -> bool:
        """Restore megolm session keys from the homeserver Online Key Backup.

        ``recovery_key`` defaults to the env var
        ``BEEPER_KEY_BACKUP_RECOVERY_KEY``. If neither is set, returns
        False without touching the network — Phase 1 ships with this
        intentionally absent until the operator provides it.

        Returns True on success (also if already restored — idempotent),
        False if no key was supplied. Raises ``KeyBackupRestoreError``
        for genuine failures (auth, network, libolm).

        We probe a few possible nio API names so this code keeps working
        across nio releases:
            * ``restore_room_keys_from_backup`` (newer)
            * ``import_room_keys`` + ``room_keys_get`` (older)
        If neither is present we log a clear warning and return False.
        """
        if self._keys_restored:
            return True
        rk = recovery_key or os.environ.get("BEEPER_KEY_BACKUP_RECOVERY_KEY")
        if not rk:
            self.log.info(
                "Matrix key backup: no recovery key (BEEPER_KEY_BACKUP_RECOVERY_KEY) — skipping",
            )
            return False

        nio_client = getattr(self.client, "_client", None)
        if nio_client is None:
            raise KeyBackupRestoreError("matrix client not logged in")

        fn: Optional[Callable[..., Awaitable[Any]]] = (
            getattr(nio_client, "restore_room_keys_from_backup", None)
        )
        try:
            if fn is not None:
                resp = await fn(rk)  # type: ignore[misc]
            else:
                # Older nio: room_keys_get gives ciphertext, we then decrypt
                # locally and feed import_keys. We don't try to roll our
                # own crypto here — surface a clear error instead.
                raise KeyBackupRestoreError(
                    "matrix-nio AsyncClient has no restore_room_keys_from_backup; "
                    "upgrade matrix-nio[e2e] to a version that supports key backup",
                )
        except KeyBackupRestoreError:
            raise
        except Exception as exc:
            raise KeyBackupRestoreError(
                f"key backup restore failed: {exc!r}",
            ) from exc

        self._keys_restored = True
        self.log.info(
            "Matrix key backup: restored for %s (resp=%r)",
            getattr(self.client, "user_id", "?"), resp,
        )
        return True

    # ── per-event decrypt ─────────────────────────────────────────────

    async def decrypt_event(
        self,
        event_id: str,
        raw_content: dict,
        room_id: Optional[str] = None,
    ) -> tuple[Optional[str], dict]:
        """Decrypt one event's payload.

        Returns ``(plaintext_body, decrypted_content_dict)``. A ``None``
        body just means the inner event has no ``body`` key (e.g. a
        reaction or sticker). Raises ``MatrixDecryptionError`` if the
        underlying client refuses to decrypt — caller / circuit breaker
        decides what to do next.
        """
        nio_client = getattr(self.client, "_client", None)
        if nio_client is None:
            raise MatrixDecryptionError("matrix client not logged in")
        decrypt_fn = getattr(nio_client, "decrypt_event", None)
        if decrypt_fn is None:
            raise MatrixDecryptionError(
                "matrix-nio AsyncClient missing decrypt_event",
            )

        try:
            result = await _maybe_await(decrypt_fn(raw_content))
        except Exception as exc:
            raise MatrixDecryptionError(
                f"decrypt_event({event_id}) failed: {exc!r}",
            ) from exc

        # nio typically returns an Event with .source (full dict) and
        # .body for messages. Be tolerant of both dict and object returns.
        if isinstance(result, dict):
            decrypted = result
            body = decrypted.get("content", {}).get("body")
        else:
            body = getattr(result, "body", None)
            decrypted = (
                getattr(result, "source", None)
                or _event_to_dict(result)
            )

        if not isinstance(decrypted, dict) or not decrypted:
            raise MatrixDecryptionError(
                f"decrypt_event({event_id}) returned non-dict or empty: {type(decrypted)!r}",
            )
        return body, decrypted

    # ── batch ─────────────────────────────────────────────────────────

    async def decrypt_pending(self, limit: int = 100) -> dict:
        """Fetch up to ``limit`` undecrypted rows and decrypt each.

        Returns ``{"attempted": int, "decrypted": int, "failed": int,
        "skipped": int}``. Never raises (except CircuitOpenError) — a
        single bad event must not stop the rest. ``skipped`` counts
        cycles when the breaker is OPEN.
        """
        stats = {"attempted": 0, "decrypted": 0, "failed": 0, "skipped": 0}
        if self.writer is None:
            return stats
        get_pending = getattr(self.writer, "get_undecrypted_events", None)
        update = getattr(self.writer, "update_decrypted", None)
        if get_pending is None or update is None:
            self.log.warning(
                "decrypt_pending: writer missing get_undecrypted_events/update_decrypted",
            )
            return stats

        rows = await get_pending(limit)
        for row in rows or ():
            stats["attempted"] += 1
            event_id = row["event_id"] if "event_id" in row else row.get("id")
            raw = row.get("raw_content") or row.get("content") or {}
            room_id = row.get("room_id")
            try:
                body, decrypted = await self._breaker.call(
                    lambda: self.decrypt_event(event_id, raw, room_id)
                )
            except CircuitOpenError:
                stats["skipped"] += 1
                self.log.warning(
                    "Matrix decrypt circuit OPEN — skipping remaining %d rows",
                    len(rows) - stats["attempted"] + 1,
                )
                # Skip the rest of this batch — don't keep banging on it.
                stats["skipped"] += max(0, len(rows) - stats["attempted"])
                break
            except MatrixDecryptionError as exc:
                stats["failed"] += 1
                self.log.warning(
                    "Matrix decrypt failed for %s: %s", event_id, exc,
                )
                continue

            try:
                await update(event_id, body, decrypted)
            except Exception as exc:  # pragma: no cover - DB error path
                stats["failed"] += 1
                self.log.error(
                    "Matrix decrypt: writer.update_decrypted(%s) failed: %r",
                    event_id, exc,
                )
                continue
            stats["decrypted"] += 1
        return stats

    # ── observability ─────────────────────────────────────────────────

    def breaker_state(self) -> dict:
        """Snapshot of the underlying circuit breaker — for /metrics."""
        return self._breaker.stats()


# ── helpers ───────────────────────────────────────────────────────────────


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it's awaitable, otherwise return it."""
    if hasattr(value, "__await__"):
        return await value
    return value


def _event_to_dict(event: Any) -> dict:
    """Best-effort conversion of a nio Event to a plain dict."""
    out: dict = {}
    for attr in ("event_id", "sender", "server_timestamp", "type"):
        v = getattr(event, attr, None)
        if v is not None:
            out[attr] = v
    body = getattr(event, "body", None)
    if body is not None:
        out["content"] = {"body": body}
    return out


__all__ = [
    "KeyBackupRestoreError",
    "MatrixDecryptionError",
    "MatrixDecryptionService",
]
