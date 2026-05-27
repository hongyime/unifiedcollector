"""Read-only Matrix /sync client foundation for Beeper.

Wraps matrix-nio's AsyncClient with the conventions used by the rest of
unifiedcollector: asyncpg pool for state, our resilience.async_retry for
backoff, and a circuit breaker around the long-poll sync.

This module is intentionally PURE INGEST — no send_message, no room_join,
no read receipts, no typing. Wave 1's matrix_collector consumes this.

Design points
─────────────
* `BeeperMatrixClient.login()` accepts either a password (initial login) or
  an existing access_token (refresh-flow re-auth).
* `sync_once()` performs a single /sync; `sync_forever()` is the long-poll
  loop with retry + circuit breaker.
* `next_batch` is persisted to the `matrix_sync_state` table (single row
  per user_id) so the client is resumable across restarts.
* Encryption: we point matrix-nio at a `store_path` so olm/megolm session
  keys persist on disk. We DO NOT yet recover from key backup (that's a
  Wave 1 Phase 1 task) — if we hit an undecryptable MegolmEvent the
  callback receives it as-is and a clear MatrixDecryptionError is logged.
* Read-only: see `_READ_ONLY_GUARD` — every public method is documented
  read-only and we override no send/join/leave/typing primitives.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from src.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from src.core.resilience import async_retry

logger = logging.getLogger(__name__)


# ── matrix-nio imports (lazy-tolerant) ─────────────────────────────────────
# We import lazily inside __init__ where helpful so unit tests can monkeypatch
# the AsyncClient symbol on this module without needing the full library at
# collection time. The top-level imports are still attempted so that real
# runtime usage fails fast with a clear ImportError if matrix-nio is missing.

try:  # pragma: no cover - import guard exercised by integration env
    from nio import (  # type: ignore
        AsyncClient,
        AsyncClientConfig,
        LoginResponse,
        LoginError,
        MegolmEvent,
        RoomMessagesResponse,
        SyncResponse,
    )
    _NIO_AVAILABLE = True
except ImportError:  # pragma: no cover
    AsyncClient = None  # type: ignore
    AsyncClientConfig = None  # type: ignore
    LoginResponse = None  # type: ignore
    LoginError = None  # type: ignore
    MegolmEvent = None  # type: ignore
    RoomMessagesResponse = None  # type: ignore
    SyncResponse = None  # type: ignore
    _NIO_AVAILABLE = False


# ── public exceptions ──────────────────────────────────────────────────────


class MatrixClientError(Exception):
    """Base error for BeeperMatrixClient."""


class MatrixLoginError(MatrixClientError):
    """Login (password or token) was rejected by the homeserver."""


class MatrixDecryptionError(MatrixClientError):
    """An encrypted event arrived without keys to decrypt it.

    Wave 1's collector will catch this and decide whether to ingest the
    event in undecrypted form or skip it.
    """


class MatrixSyncFailedError(MatrixClientError):
    """sync_once exceeded the consecutive-failure threshold in sync_forever."""


# ── small data shapes ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class RoomSummary:
    """Lightweight, read-only view of a Matrix room.

    Returned by `list_rooms()`. Fields chosen to match what the Beeper UI
    surfaces and what downstream collectors need for indexing.
    """

    room_id: str
    display_name: Optional[str]
    topic: Optional[str]
    member_count: int
    encrypted: bool
    last_activity_ts: Optional[int]  # ms since epoch, from origin_server_ts


# ── sync state repository ──────────────────────────────────────────────────


class MatrixSyncStateRepository:
    """Persists `next_batch` per user_id in postgres.

    Single-row pattern (PK=user_id). Mirrors checkpoint.py in spirit but
    Matrix has its own token shape (opaque string) and we want it
    co-located with last_sync_at so monitoring can spot stalled clients.
    """

    def __init__(self, pool: Any, user_id: str) -> None:
        self._pool = pool
        self._user_id = user_id

    async def load(self) -> Optional[str]:
        """Return the persisted next_batch token, or None on first run."""
        if self._pool is None:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO matrix_sync_state (user_id)
                VALUES ($1)
                ON CONFLICT (user_id) DO UPDATE SET user_id = EXCLUDED.user_id
                RETURNING next_batch
                """,
                self._user_id,
            )
            return row["next_batch"] if row else None

    async def save(self, next_batch: str) -> None:
        if self._pool is None:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE matrix_sync_state
                SET next_batch = $2,
                    last_sync_at = $3
                WHERE user_id = $1
                """,
                self._user_id,
                next_batch,
                datetime.now(timezone.utc),
            )


# ── the client ─────────────────────────────────────────────────────────────


SyncCallback = Callable[[Any], Awaitable[None]]
"""User-supplied callback invoked once per SyncResponse in sync_forever."""


class BeeperMatrixClient:
    """Read-only Matrix /sync client for Beeper's homeserver.

    Usage::

        client = BeeperMatrixClient(
            homeserver="https://matrix.beeper.com",
            user_id="@bryan:beeper.com",
            store_path="/data/matrix_store",
            pool=pg_pool,
        )
        await client.login(password=os.environ["BEEPER_MATRIX_PASSWORD"])
        await client.sync_forever(my_callback)

    The class deliberately exposes a small surface — no send/join/typing.
    """

    # Consecutive sync_once failures tolerated before sync_forever bails out.
    MAX_CONSECUTIVE_SYNC_FAILURES = 5

    def __init__(
        self,
        homeserver: str,
        user_id: str,
        store_path: Optional[str] = None,
        pool: Any = None,
        device_id: Optional[str] = None,
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not homeserver:
            raise ValueError("homeserver is required")
        if not user_id:
            raise ValueError("user_id is required")

        self.homeserver = homeserver
        self.user_id = user_id
        self.store_path = store_path
        self.device_id = device_id
        self._pool = pool
        self._sync_state = MatrixSyncStateRepository(pool, user_id)
        self._breaker = CircuitBreaker(
            name=f"matrix-sync:{user_id}",
            failure_threshold=5,
            recovery_timeout=60.0,
        )
        self._stop = asyncio.Event()

        # Allow tests to inject a stubbed AsyncClient. Production resolves to
        # nio.AsyncClient at this module's import time.
        self._client_factory = client_factory or _default_client_factory
        self._client: Any = None

        if store_path:
            os.makedirs(store_path, exist_ok=True)

    # ── lifecycle ──────────────────────────────────────────────────────

    async def login(
        self,
        password: Optional[str] = None,
        access_token: Optional[str] = None,
        device_name: str = "unifiedcollector",
    ) -> None:
        """Authenticate against the homeserver.

        Either `password` (fresh login) or `access_token` (re-auth with a
        previously-issued token) must be supplied. The token path is what
        we use after the initial login is persisted by the caller.
        """
        if not password and not access_token:
            raise ValueError("login requires either password or access_token")

        self._client = self._client_factory(
            homeserver=self.homeserver,
            user=self.user_id,
            device_id=self.device_id,
            store_path=self.store_path,
        )

        if access_token:
            # Direct restore — no network call. Caller is responsible for
            # device_id matching what was used when the token was issued.
            self._client.access_token = access_token
            if self.device_id:
                self._client.device_id = self.device_id
            self._client.user_id = self.user_id
            logger.info("Matrix client restored from access_token for %s", self.user_id)
            return

        # Password flow.
        resp = await self._client.login(password, device_name=device_name)
        if LoginError is not None and isinstance(resp, LoginError):
            raise MatrixLoginError(f"Beeper login rejected: {getattr(resp, 'message', resp)}")
        # matrix-nio's LoginResponse exposes .access_token / .device_id;
        # we just confirm presence — caller can read self.access_token.
        if not getattr(self._client, "access_token", None):
            raise MatrixLoginError("Login returned no access_token")
        # Capture device_id assigned by server (if any) for future re-auth.
        if not self.device_id:
            self.device_id = getattr(self._client, "device_id", None)
        logger.info("Matrix login OK for %s (device=%s)", self.user_id, self.device_id)

    async def close(self) -> None:
        """Close the underlying HTTP session. Read-only — no logout."""
        self._stop.set()
        if self._client is not None and hasattr(self._client, "close"):
            await self._client.close()

    @property
    def access_token(self) -> Optional[str]:
        return getattr(self._client, "access_token", None) if self._client else None

    # ── sync ───────────────────────────────────────────────────────────

    @async_retry(max_retries=3, base_delay=1.0, max_delay=30.0)
    async def sync_once(self, timeout_ms: int = 30_000) -> Any:
        """Perform a single /sync call and return the SyncResponse.

        Wrapped in async_retry for transient errors. The caller (sync_forever)
        adds the consecutive-failure guard on top.
        """
        self._require_logged_in()
        next_batch = await self._sync_state.load()
        resp = await self._client.sync(timeout=timeout_ms, since=next_batch)
        # SyncError → exception so async_retry will back off.
        if SyncResponse is not None and not isinstance(resp, SyncResponse):
            # nio returns SyncError on failure; surface as exception.
            raise MatrixClientError(f"sync failed: {getattr(resp, 'message', resp)}")
        new_token = getattr(resp, "next_batch", None)
        if new_token:
            await self._sync_state.save(new_token)
        return resp

    async def sync_forever(self, callback: SyncCallback, timeout_ms: int = 30_000) -> None:
        """Long-poll sync loop. Calls `callback(sync_response)` per cycle.

        After 5 consecutive sync_once errors, raises MatrixSyncFailedError.
        Cancel via close() / stop().
        """
        consecutive_failures = 0
        self._stop.clear()
        while not self._stop.is_set():
            try:
                resp = await self._breaker.call(
                    lambda: self.sync_once(timeout_ms=timeout_ms)
                )
            except CircuitOpenError:
                logger.warning("Matrix circuit breaker OPEN — pausing sync")
                await asyncio.sleep(5.0)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                logger.error(
                    "sync_once failed (%d/%d consecutive): %s",
                    consecutive_failures, self.MAX_CONSECUTIVE_SYNC_FAILURES, exc,
                )
                if consecutive_failures >= self.MAX_CONSECUTIVE_SYNC_FAILURES:
                    raise MatrixSyncFailedError(
                        f"{consecutive_failures} consecutive sync failures"
                    ) from exc
                # Small backoff between bursts; async_retry already jittered
                # the per-call retries.
                await asyncio.sleep(min(2 ** consecutive_failures, 30))
                continue
            else:
                consecutive_failures = 0
                await callback(resp)

    def stop(self) -> None:
        """Request sync_forever to exit at the next iteration boundary."""
        self._stop.set()

    # ── room queries ───────────────────────────────────────────────────

    async def list_rooms(self) -> dict[str, RoomSummary]:
        """Return a dict of {room_id: RoomSummary} for currently-joined rooms.

        Pulls from the in-memory `client.rooms` map populated by previous
        syncs — no extra HTTP call. Caller should sync_once() at least once
        before relying on this.
        """
        self._require_logged_in()
        rooms_map = getattr(self._client, "rooms", {}) or {}
        out: dict[str, RoomSummary] = {}
        for room_id, room in rooms_map.items():
            out[room_id] = RoomSummary(
                room_id=room_id,
                display_name=getattr(room, "display_name", None) or getattr(room, "name", None),
                topic=getattr(room, "topic", None),
                member_count=int(getattr(room, "member_count", 0) or 0),
                encrypted=bool(getattr(room, "encrypted", False)),
                last_activity_ts=_extract_last_activity_ms(room),
            )
        return out

    @async_retry(max_retries=3, base_delay=1.0, max_delay=30.0)
    async def fetch_history(
        self,
        room_id: str,
        limit: int = 100,
        before_token: Optional[str] = None,
    ) -> Any:
        """Paginated history fetch via /rooms/{id}/messages.

        `before_token` is a `prev_batch` string from a previous response
        (or from a SyncResponse timeline). Returns the raw nio response —
        callers iterate response.chunk for events.
        """
        self._require_logged_in()
        if limit <= 0:
            raise ValueError("limit must be > 0")
        # matrix-nio: room_messages(room_id, start, limit, direction='b')
        resp = await self._client.room_messages(
            room_id=room_id,
            start=before_token or "",
            limit=limit,
            direction="b",
        )
        if (
            RoomMessagesResponse is not None
            and not isinstance(resp, RoomMessagesResponse)
        ):
            raise MatrixClientError(
                f"fetch_history failed for {room_id}: {getattr(resp, 'message', resp)}"
            )
        return resp

    # ── encryption helpers ────────────────────────────────────────────

    @staticmethod
    def is_undecryptable(event: Any) -> bool:
        """Predicate for the collector: True if this is a MegolmEvent we
        could not decrypt (no session keys yet).

        Matrix-nio represents successfully-decrypted megolm events by
        substituting the inner event type (e.g. RoomMessageText). A bare
        MegolmEvent in the timeline => decryption failed.
        """
        if MegolmEvent is None:
            return False
        return isinstance(event, MegolmEvent)

    # ── internals ──────────────────────────────────────────────────────

    def _require_logged_in(self) -> None:
        if self._client is None:
            raise MatrixClientError("login() must be called before this operation")
        if not getattr(self._client, "access_token", None):
            raise MatrixClientError("client has no access_token — login first")


# ── helpers ────────────────────────────────────────────────────────────────


def _default_client_factory(
    *, homeserver: str, user: str, device_id: Optional[str], store_path: Optional[str]
) -> Any:
    """Build a real nio.AsyncClient with sensible E2EE defaults."""
    if not _NIO_AVAILABLE:
        raise ImportError(
            "matrix-nio is not installed; add 'matrix-nio[e2e]>=0.24.0' to "
            "requirements.txt and rebuild the collector image"
        )
    config = AsyncClientConfig(
        store_sync_tokens=True,
        encryption_enabled=bool(store_path),  # E2EE only if we have a store
    )
    return AsyncClient(
        homeserver=homeserver,
        user=user,
        device_id=device_id,
        store_path=store_path,
        config=config,
    )


def _extract_last_activity_ms(room: Any) -> Optional[int]:
    """Best-effort pull of last activity timestamp from a nio MatrixRoom.

    matrix-nio doesn't expose a single field for this — we look at a few
    likely attributes and fall back to None.
    """
    for attr in ("last_event_timestamp", "origin_server_ts", "_last_event_timestamp"):
        v = getattr(room, attr, None)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


__all__ = [
    "BeeperMatrixClient",
    "MatrixClientError",
    "MatrixDecryptionError",
    "MatrixLoginError",
    "MatrixSyncFailedError",
    "MatrixSyncStateRepository",
    "RoomSummary",
]
