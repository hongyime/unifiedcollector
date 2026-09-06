"""Per-account Telegram session / worker primitives.

Extracted from :mod:`src.collectors.telegram` (PERF-004 sub-plan 4C step 3).
The exception types ``EntityUnresolvable`` / ``EntityResolveDeferred`` live
here alongside :class:`TelegramWorker` — they are the only signalling the
worker uses to hand a target back to the collector's dispatch logic.

Re-exported from :mod:`src.collectors.telegram` so downstream imports (the
scheduler, the docker health CLI, the tests) keep working unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from src.collectors.telegram.helpers import _is_flood_wait
from src.core.circuit_breaker import CircuitBreaker, CircuitOpenError

if TYPE_CHECKING:  # pragma: no cover — forward-ref hints only
    from src.collectors.telegram import TelegramCollector


# Log under the parent module name so records still surface as
# ``src.collectors.telegram`` — parity with pre-extraction behaviour.
logger = logging.getLogger("src.collectors.telegram")


class EntityUnresolvable(Exception):
    """No connected account can resolve a chat entity.

    Terminal for the spider queue: every one of the N accounts was asked and none
    is a member (left / deleted / private channel we're not in). Distinct from a
    TRANSIENT resolve failure (a disconnected account or a network timeout), which
    a later cycle should retry — those are re-raised as their original exception so
    _process_spider_queue treats them as retryable, not permanently 'unresolvable'.
    """


class EntityResolveDeferred(Exception):
    """Resolve should be retried later without penalizing the current account."""


# ──────────────────────────────────────────────────────────────────────────
# Worker
# ──────────────────────────────────────────────────────────────────────────


class SessionState(Enum):
    INIT = "init"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FLOOD_WAIT = "flood_wait"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class TelegramWorker:
    """Per-account Telegram worker. Owns its own TelegramClient + session."""

    def __init__(self, parent: "TelegramCollector", account, worker_id: int):
        self.parent = parent
        self.account = account
        self.worker_id = worker_id
        self.client = None
        self.account_id = getattr(account, "id", None)  # for backfill rate-limit
        self.state = SessionState.INIT
        self._claimed_chats: set[str] = set()  # chats this worker is assigned
        # Per-account circuit breaker: trips after 5 consecutive Telethon
        # errors and stays open for 60s before allowing a single probe.
        # FloodWaitError is handled separately by record_flood_wait, so
        # excluding it from the breaker prevents legitimate rate-limits
        # from tripping the circuit.
        self.breaker = CircuitBreaker(
            name=f"telegram[{account.name}]",
            failure_threshold=5,
            recovery_timeout=60.0,
        )

    async def connect(self):
        from telethon import TelegramClient  # NOT telethon.sync!
        from telethon.sessions import StringSession
        session_dir = Path("sessions")
        session_dir.mkdir(parents=True, exist_ok=True)

        api_id = int(self.account.credentials.get("api_id") or os.getenv("TELEGRAM_API_ID", "0"))
        api_hash = self.account.credentials.get("api_hash") or os.getenv("TELEGRAM_API_HASH", "")
        session_val = self.account.credentials.get("session", "")
        # StringSession strings are long base64 — detect by length (>100 chars)
        if session_val and len(session_val) > 100:
            session_file = StringSession(session_val)
        elif session_val and Path(session_val).exists():
            session_file = str(session_val)
        elif session_val:
            # ENV/DB gave an explicit session path but the file is missing.
            # Do NOT silently fall back to a name-based path — that creates a
            # fresh UNAUTHORIZED session and Telethon's start() then blocks on
            # interactive stdin (=> "EOF when reading a line"). Fail loudly so
            # the session-file/volume mismatch is obvious.
            raise FileNotFoundError(
                f"Telegram session file not found: {session_val!r} "
                f"(account={self.account.name}). The authorized .session file "
                f"must be present in the sessions volume."
            )
        else:
            session_file = str(session_dir / self.account.name)

        logger.info(
            "[worker=%d account=%s] Connecting Telegram (session=%s)",
            self.worker_id, self.account.name, session_file,
        )
        # Self-heal a CORRUPT session before anything touches it (2026-06-21:
        # OOM/hard-kill mid-write corrupted the entities btree -> "malformed" ->
        # worker permanently disconnected). ensure_healthy_session() .recover's it
        # in place, preserving auth_key (no re-login). No-op when already healthy.
        if isinstance(session_file, str):
            try:
                from src.core.session_repair import ensure_healthy_session
                if not ensure_healthy_session(session_file):
                    logger.error(
                        "[worker=%d account=%s] session could not be repaired — "
                        "may need re-auth", self.worker_id, self.account.name,
                    )
            except Exception:
                logger.warning(
                    "[worker=%d account=%s] session_repair raised",
                    self.worker_id, self.account.name, exc_info=True,
                )
        # Harden the .session SQLite against "database is locked" BEFORE Telethon
        # opens it. The files live on the WSL2 Docker volume (slow fsync), so the
        # default journal mode + 5s busy_timeout made Telethon's concurrent session
        # access (update loop writing while queries read) crash the keepalive loop.
        # Setting journal_mode=WAL via a direct sqlite3 connection is PERSISTENT
        # (stored in the DB header), so it sticks for every subsequent Telethon
        # connection — unlike the previous post-connect attempt, which never engaged
        # (no -wal files were ever created). Only for file-backed sessions.
        if isinstance(session_file, str):
            _spath = session_file if session_file.endswith(".session") else f"{session_file}.session"
            if os.path.exists(_spath):
                try:
                    import sqlite3 as _sqlite
                    _c = _sqlite.connect(_spath, timeout=30)
                    _c.execute("PRAGMA journal_mode=WAL")
                    _c.execute("PRAGMA busy_timeout=30000")
                    _c.commit()
                    _c.close()
                    logger.info(
                        "[worker=%d account=%s] session SQLite set to WAL (persistent)",
                        self.worker_id, self.account.name,
                    )
                except Exception as _pragma_err:
                    logger.warning(
                        "[worker=%d account=%s] could not set WAL on session: %s",
                        self.worker_id, self.account.name, _pragma_err,
                    )
        raw_client = None
        try:
            self.state = SessionState.CONNECTING
            raw_client = TelegramClient(session_file, api_id, api_hash)
            # Use connect() + is_user_authorized() instead of start(): start()
            # falls back to interactive stdin prompts when a session is not
            # authorized, which blocks forever in a container (=> "EOF when
            # reading a line"). We never want that — fail cleanly instead.
            await raw_client.connect()
            # WAL (set pre-connect) handles concurrent reader+writer, but Telethon's
            # OWN connection keeps the default 5s busy_timeout, so heavy writers like
            # collect_dialogs (caching all dialog entities) still threw "database is
            # locked". Raise busy_timeout on Telethon's live connection so it WAITS
            # for the lock (up to 30s) instead of failing.
            try:
                _conn = getattr(raw_client.session, "_conn", None)
                if _conn is not None:
                    _conn.execute("PRAGMA busy_timeout=30000")
                    logger.info(
                        "[worker=%d account=%s] telethon conn busy_timeout=30s set",
                        self.worker_id, self.account.name,
                    )
            except Exception as _bt_err:
                logger.warning(
                    "[worker=%d account=%s] could not set busy_timeout: %s",
                    self.worker_id, self.account.name, _bt_err,
                )
            from src.core.readonly_client import ReadOnlyTelegramClient
            self.client = ReadOnlyTelegramClient(raw_client)
            if not await self.client.is_user_authorized():
                raise RuntimeError(
                    f"Telegram session for account={self.account.name} is not "
                    f"authorized. Re-auth via /startcollector or restore the "
                    f"authorized .session file."
                )
            self.state = SessionState.CONNECTED
            try:
                me = await self.client.get_me()
                me_label = f"id={getattr(me, 'id', '?')} user={getattr(me, 'username', None)} phone={getattr(me, 'phone', None)}"
            except Exception:
                me_label = "<unknown>"
            logger.info(
                "[worker=%d account=%s] Telegram client CONNECTED (%s)",
                self.worker_id, self.account.name, me_label,
            )
        except asyncio.CancelledError:
            self.state = SessionState.ERROR
            if raw_client is not None and self.client is None:
                try:
                    await raw_client.disconnect()
                except Exception:
                    pass
            raise
        except Exception as e:
            self.state = SessionState.ERROR
            if raw_client is not None and self.client is None:
                try:
                    await raw_client.disconnect()
                except Exception:
                    pass
            err_text = str(e).lower()
            if "auth" in err_text or "session" in err_text or "phone" in err_text or "key" in err_text:
                kind = "auth_failure"
            elif "timeout" in err_text:
                kind = "timeout"
            else:
                kind = "network_error"
            self.parent.account_pool.record_error_classified(self.account.name, kind)
            logger.error(
                "[worker=%d account=%s] Connect failed (%s): %s",
                self.worker_id, self.account.name, kind, e,
            )
            raise

    async def disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.state = SessionState.DISCONNECTED
            self.client = None

    async def run_targets(self, targets: list[str]):
        """Process the list of targets assigned to this worker."""
        for target in targets:
            if self.parent._stop.is_set():
                break
            unresolvable_until = self.parent._target_unresolvable_until(target)
            if unresolvable_until:
                remaining = max(0, int(unresolvable_until - time.monotonic()))
                logger.info(
                    "[worker=%d account=%s] skipping telegram/%s: unresolvable target cache active for %ds",
                    self.worker_id, self.account.name, target, remaining,
                )
                continue
            logger.info(
                "[worker=%d account=%s] Collecting telegram/%s",
                self.worker_id, self.account.name, target,
            )
            try:
                await self.breaker.call(
                    lambda t=target: self.parent._collect_chat(self, t)
                )
                await self.parent.checkpoint.save_progress(target)
                self.parent.account_pool.record_success(self.account.name)
            except CircuitOpenError as e:
                logger.warning(
                    "[worker=%d account=%s] circuit open, skipping %s: %s",
                    self.worker_id, self.account.name, target, e,
                )
                try:
                    await self.parent.send_to_dlq(target, target, f"circuit_open: {e}")
                except Exception:
                    pass
            except EntityUnresolvable as e:
                self.parent._remember_unresolvable_target(target)
                await self.parent._mark_collection_target_error(
                    target,
                    "unresolvable",
                    str(e),
                )
                logger.info(
                    "[worker=%d account=%s] telegram/%s is unresolvable by connected accounts: %s",
                    self.worker_id, self.account.name, target, e,
                )
                try:
                    await self.parent.send_to_dlq(target, target, f"unresolvable: {e}")
                except Exception:
                    pass
            except EntityResolveDeferred as e:
                logger.info(
                    "[worker=%d account=%s] deferred telegram/%s without account penalty: %s",
                    self.worker_id, self.account.name, target, e,
                )
                await self.parent._mark_collection_target_error(
                    target,
                    "pending",
                    str(e),
                )
            except Exception as e:
                if _is_flood_wait(e):
                    await self.parent._handle_flood_wait(self, e)
                else:
                    err_text = str(e).lower()
                    if "auth" in err_text or "unauthorized" in err_text or "session" in err_text:
                        kind = "auth_failure"
                    elif "timeout" in err_text:
                        kind = "timeout"
                    elif "privat" in err_text or "forbidden" in err_text or "channel_private" in err_text:
                        kind = "privacy_error"
                    else:
                        kind = "network_error"
                    self.parent.account_pool.record_error_classified(self.account.name, kind)
                    logger.error(
                        "[worker=%d account=%s] Failed telegram/%s (%s): %s",
                        self.worker_id, self.account.name, target, kind, e,
                    )
                    try:
                        await self.parent.send_to_dlq(target, target, str(e))
                    except Exception:
                        pass
