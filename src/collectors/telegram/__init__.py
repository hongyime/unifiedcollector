"""Unified Telegram collector — Wave 2 Batch E.

Ports `telegramcollector/services/collector/{backfill_worker,realtime_worker,...}.py`
+ cherry-picks `telegramtoolkit/src/{core/scan_targets,managers/download_profile_photos,
managers/processors/user_analyzer_processor}.py` into a single BaseCollector subclass.

Public surface (called by scheduler / cron):
    - run(targets)          BaseCollector lifecycle entry
    - collect(targets)      per-cycle parallel collect across worker pool
    - collect_realtime()    spawn @client.on(NewMessage) handlers + run forever
    - backfill_chat(chat_id, target_depth=N, max_iterations=M)
                            cursor-based historical pagination (newest -> oldest)
    - collect_dialogs()     iter_dialogs across all workers; upsert telegram_chats
    - collect_chat_members(chat_id)
                            iter_participants → telegram_chat_members upsert
                            (called by daily 03:00 SGT cron — see PRD memory)
    - collect_user_profile(user_id)
                            user metadata + profile photos
    - download_message_media(message_id)
                            single-message media download via Telethon → core.media_download

Anything outbound (send/reply/edit/delete/forward, bot commands, web UI,
bulk_sender) is DROPPED per Wave 0 spec.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from datetime import datetime, date, timezone
from enum import Enum
from pathlib import Path

from src.core.base_collector import BaseCollector
from src.collectors.telegram.parse import (
    detect_message_type as _parse_detect_message_type,
    extract_file_info as _parse_extract_file_info,
    ext_from_mime as _parse_ext_from_mime,
    _MIME_EXT_MAP as _parse_MIME_EXT_MAP,
)
from src.core.account_pool import AccountPool
from src.core.bot_pool import BotPool
from src.core.hub_notifier import HubNotifier, NotifyCategory
from src.core.discovered_links import persist_discovered_links
from src.core.file_naming import sanitize_name
from src.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from src.core.proximity import refresh_account_proximity_cache
from src.core.raw_archive import report_raw_archive_result
from src.core.rate_limit_events import record_rate_limit_event
from src.core.user_change_tracker import (
    UserChangeTracker,
    TELEGRAM_TRACKED_FIELDS,
)
from src.core.vault import VAULT_ROOT, write_atomic_artifact, write_raw_payload

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# JSON helpers — Telethon to_dict() emits bytes (access hashes) + datetime.
# ──────────────────────────────────────────────────────────────────────────


def _tg_json(obj):
    """JSON default for Telethon objects.

    Handles bytes (access hashes), datetime, and any other non-serializable
    types — never raises so message ingest never fails on serialization.
    """
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def _tg_jsonb(obj) -> str:
    """Serialize a value for a Postgres jsonb column.

    NOTE: `_tg_json` is the json.dumps `default=` *callback*, not a serializer —
    calling it directly returns ``str(obj)`` (single-quoted Python repr), which
    is INVALID JSON and makes the `::jsonb` cast fail silently. This wraps it
    correctly so dicts/lists become real JSON. (Fixes silent loss of
    telegram_reaction_counts / telegram_polls rows.)
    """
    return json.dumps(obj, default=_tg_json, ensure_ascii=False)


def _tier1_raw_archives_enabled() -> bool:
    raw = os.getenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _telethon_payload(obj) -> dict:
    if hasattr(obj, "to_dict"):
        try:
            payload = obj.to_dict()
            if isinstance(payload, dict):
                return payload
        except Exception:
            logger.debug("Telethon to_dict failed for raw archive", exc_info=True)
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        return {
            key: value
            for key, value in vars(obj).items()
            if not key.startswith("_")
        }
    return {"repr": str(obj)}


_MIME_EXT_MAP = _parse_MIME_EXT_MAP


def _ext_from_mime(mime_type):
    return _parse_ext_from_mime(mime_type)


def _is_flood_wait(exc):
    """Detect FloodWaitError without importing telethon at module scope."""
    name = type(exc).__name__
    if name == "FloodWaitError":
        return True
    return hasattr(exc, "seconds") and "flood" in name.lower()


def _is_transient_realtime_write_error(exc) -> bool:
    """Return True for DB/network blips worth retrying on the hot realtime path."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    exc_type = type(exc)
    name = exc_type.__name__.lower()
    module = getattr(exc_type, "__module__", "").lower()
    if "asyncpg" not in module:
        return False
    return any(
        token in name
        for token in (
            "timeout",
            "connection",
            "interface",
            "cannotconnect",
            "connectiondoesnotexist",
        )
    )


class EntityUnresolvable(Exception):
    """No connected account can resolve a chat entity.

    Terminal for the spider queue: every one of the N accounts was asked and none
    is a member (left / deleted / private channel we're not in). Distinct from a
    TRANSIENT resolve failure (a disconnected account or a network timeout), which
    a later cycle should retry — those are re-raised as their original exception so
    _process_spider_queue treats them as retryable, not permanently 'unresolvable'.
    """


def _as_int(x):
    """int(x) or None — for trying a chat id as numeric then raw string."""
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _is_transient(exc):
    """A resolve/collect error that is worth retrying (vs the chat being dead).

    Connection drops, timeouts, and 'server closed'/'disconnected' blips are
    transient — the account may resolve the chat fine on the next cycle.
    """
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    blob = f"{type(exc).__name__}: {exc}".lower()
    return any(s in blob for s in (
        "disconnect", "server closed", "timeout", "connection", "not connected",
    ))


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
        except Exception as e:
            self.state = SessionState.ERROR
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


# ──────────────────────────────────────────────────────────────────────────
# Collector
# ──────────────────────────────────────────────────────────────────────────


class TelegramCollector(BaseCollector):
    SOURCE_NAME = "telegram"
    INGEST_PATH = "messaging"  # realtime messaging path (P2 review §3 provenance)

    def __init__(self):
        super().__init__()
        self._api_id = os.getenv("TELEGRAM_API_ID", "")
        self._api_hash = os.getenv("TELEGRAM_API_HASH", "")
        self._session_name = os.getenv("TELEGRAM_SESSION", "collector")
        self._workers: list[TelegramWorker] = []
        self._primary_client = None  # for HubNotifier/BotPool/etc that expect a single client
        self._sem = asyncio.Semaphore(3)
        self._batch_size = int(os.getenv("TELEGRAM_BATCH_SIZE", "100"))
        self._max_media_size = int(os.getenv("TELEGRAM_MAX_MEDIA_SIZE_MB", "50")) * 1024 * 1024
        self._backfill_enabled = os.getenv("TELEGRAM_BACKFILL_ENABLED", "true").lower() == "true"
        self._backfill_msg_per_sec = float(os.getenv("TELEGRAM_BACKFILL_MSG_PER_SEC", "20.0"))
        self._story_enabled = os.getenv("TELEGRAM_STORY_SCAN_ENABLED", "true").lower() == "true"
        self._story_interval = int(os.getenv("TELEGRAM_STORY_SCAN_INTERVAL", "300"))

        self.account_pool = AccountPool(
            default_cooldown=600.0,
            error_cooldown=1800.0,
            max_consecutive_errors=3,
        )
        self._load_accounts()
        self._bot_pool = BotPool()
        self._hub_notifier = HubNotifier()
        self._join_timestamps: deque = deque()
        self._max_joins_per_hour = int(os.getenv("TELEGRAM_MAX_JOINS_PER_HOUR", "5"))
        self._join_min_delay = int(os.getenv("TELEGRAM_JOIN_MIN_DELAY", "30"))
        self._admin_log_enabled = os.getenv("TELEGRAM_POLL_ADMIN_LOGS", "true").lower() == "true"
        self._group_join_enabled = os.getenv("TELEGRAM_GROUP_JOIN_ENABLED", "true").lower() == "true"
        _spider_accts = os.getenv("TELEGRAM_SPIDER_ACCOUNTS", "")
        self._spider_accounts: set[str] = (
            {a.strip().lower() for a in _spider_accts.split(",") if a.strip()}
            if _spider_accts else set()
        )

        # Realtime listener state — populated by collect_realtime()
        self._realtime_running = False
        self._realtime_handler_clients: set[tuple[int, int]] = set()
        self._hub_group_id: int | None = None
        try:
            self._realtime_write_attempts = max(
                1,
                int(os.getenv("TELEGRAM_REALTIME_WRITE_ATTEMPTS", "3")),
            )
        except ValueError:
            self._realtime_write_attempts = 3
        try:
            self._realtime_write_retry_delay = max(
                0.0,
                float(os.getenv("TELEGRAM_REALTIME_WRITE_RETRY_DELAY", "0.75")),
            )
        except ValueError:
            self._realtime_write_retry_delay = 0.75

        # User change tracker — wires telegram_user_changes writes into _upsert_user_full.
        # Lazy: created on first DB-bound call (since pool is set up by BaseCollector at startup).
        self._user_change_tracker: UserChangeTracker | None = None

        # Reaction-list per-message cap (Q2 decision: per-emoji per-message).
        self._reaction_user_cap = int(os.getenv("TELEGRAM_REACTION_USER_CAP", "500"))
        self._poll_vote_user_cap = int(os.getenv("TELEGRAM_POLL_VOTE_USER_CAP", "500"))

        # Discussion-group dwell range — random jitter to look human (Q3 always-leave).
        self._discussion_dwell_min = int(os.getenv("TELEGRAM_DISCUSSION_DWELL_MIN", "60"))
        self._discussion_dwell_max = int(os.getenv("TELEGRAM_DISCUSSION_DWELL_MAX", "180"))
        raw_discussion_limit = os.getenv("TELEGRAM_DISCUSSION_MESSAGE_LIMIT", "2000").strip()
        try:
            parsed_discussion_limit = int(raw_discussion_limit)
        except ValueError:
            parsed_discussion_limit = 2000
        self._discussion_message_limit: int | None = (
            None if parsed_discussion_limit <= 0 else parsed_discussion_limit
        )

        # Hot-reload task (item 4.6) — listens for new accounts via NOTIFY.
        self._hot_reload_task: asyncio.Task | None = None

    def _is_spider_allowed(self, worker: "TelegramWorker") -> bool:
        """Return whether this worker may process spider/join work."""
        if not self._spider_accounts:
            return True
        return getattr(worker.account, "name", "").lower() in self._spider_accounts

    def _load_accounts(self):
        self.account_pool.load_from_env("TELEGRAM", ["NAME", "API_ID", "API_HASH", "SESSION", "PHONE"])

    async def _load_accounts_from_db(self):
        """Load accounts from telegram_user_accounts table (item 4.5).

        This supplements env-based accounts with bot-onboarded accounts stored
        in the database. Called at startup after set_pool() provides the DB pool.
        """
        if self.pool is None:
            return

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT name, api_id, api_hash, phone, session_string
                    FROM telegram_user_accounts
                    WHERE status = 'active'
                    """
                )

            for row in rows:
                # Check if already loaded from env (env takes precedence)
                existing = [a for a in self.account_pool._accounts if a.name == row["name"]]
                if existing:
                    logger.debug("Skipping DB account %s — already loaded from env", row["name"])
                    continue

                self.account_pool.add_account(
                    name=row["name"],
                    credentials={
                        "api_id": str(row["api_id"]),
                        "api_hash": row["api_hash"],
                        "phone": row["phone"],
                        "session": row["session_string"],  # StringSession export
                    },
                )
                logger.info("Loaded account %s from telegram_user_accounts", row["name"])

        except Exception as exc:
            logger.warning("Failed to load accounts from DB: %s", exc)

    def set_pool(self, pool):
        """Override BaseCollector.set_pool to also wire UserChangeTracker."""
        super().set_pool(pool)
        # Tracker is generic — needs the asyncpg pool to write to telegram_user_changes.
        self._user_change_tracker = UserChangeTracker(pool)

    async def _mark_runtime_healthy(self, detail: str) -> None:
        """Clear stale source_health failures after a clean Telegram reconnect."""
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO source_health (source, status, last_success_at, last_error, updated_at)
                    VALUES ('telegram', 'running', NOW(), NULL, NOW())
                    ON CONFLICT (source) DO UPDATE
                    SET status='running',
                        last_success_at=NOW(),
                        last_error=NULL,
                        updated_at=NOW()
                    """,
                )
            logger.info("telegram source_health recovered: %s", detail)
        except Exception:
            logger.debug("telegram source_health recovery update failed", exc_info=True)

    @property
    def account_media_dir(self) -> Path:
        # Use session name for isolation (kept for backward compat).
        acc_name = sanitize_name(self._session_name)
        path = self.media_dir / f"session_{acc_name}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _account_media_dir_for(self, worker: "TelegramWorker") -> Path:
        acc_name = sanitize_name(worker.account.name)
        path = self.media_dir / f"session_{acc_name}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    async def _spawn_workers(self) -> list[TelegramWorker]:
        """Connect one TelegramClient per loaded account, sequentially with delays.
        
        We connect sequentially (not in parallel) because Telegram rate-limits
        multiple concurrent connections from the same IP. A small delay between
        connections avoids triggering this limit.
        """
        accounts = list(self.account_pool._accounts)  # snapshot
        if not accounts:
            logger.error("No Telegram accounts in pool — cannot start workers")
            return []

        workers = [TelegramWorker(self, acc, idx) for idx, acc in enumerate(accounts)]
        live: list[TelegramWorker] = []
        
        for w in workers:
            try:
                await asyncio.wait_for(w.connect(), timeout=45)
                live.append(w)
                # 15s between connections — prevents Telegram treating rapid
                # parallel logins as a security event and revoking auth keys.
                if len(live) < len(workers):
                    await asyncio.sleep(15)
            except asyncio.TimeoutError:
                logger.error(
                    "[worker=%d account=%s] connect timed out after 45s",
                    w.worker_id, w.account.name,
                )
            except Exception as e:
                logger.error(
                    "[worker=%d account=%s] failed to connect: %s",
                    w.worker_id, w.account.name, e,
                )

        if live:
            self._primary_client = live[0].client
        # P4-5: session-volume drift guard. Authoritative sessions are host files
        # synced into the named volume by the boot script; a manual `compose up`
        # bypasses that sync and can drift to stale/unauthorized sessions ->
        # silent auth failure. Log LOUDLY (not INFO) when any worker failed to
        # connect, naming the drifted accounts, so the gap is unmissable.
        live_names = {w.account.name for w in live}
        failed = [w.account.name for w in workers if w.account.name not in live_names]
        if failed:
            logger.error(
                "TELEGRAM SESSION DRIFT: only %d/%d worker(s) connected. "
                "UNAUTHORIZED/UNREACHABLE accounts: %s. The boot session-sync was "
                "likely bypassed (manual `compose up`?). Re-run the boot script or "
                "restore authorized .session files.",
                len(live), len(workers), ", ".join(sorted(failed)),
            )
        else:
            logger.info(
                "Telegram parallel mode: %d/%d worker(s) connected",
                len(live), len(workers),
            )
            await self._mark_runtime_healthy("all configured workers connected")
        return live

    async def _auto_backfill_new_accounts(self) -> None:
        """Auto-discover and backfill new Telegram accounts (item 2.4).

        For each connected worker, we check if their account name exists in a
        tracking set (`telegram_known_accounts` key-value in DB metadata, or
        just by checking if any `telegram_chats` rows were collected by them).

        For simplicity, we use a lightweight approach: check if we've seen ANY
        message from this account's phone number. If not → new account →
        run `collect_dialogs()` which enumerates all their chats.

        The discovered chats get upserted into `telegram_chats` and enqueued
        into `telegram_spider_queue` for full backfill.
        """
        for worker in self._workers:
            account_name = worker.account.name
            phone = worker.account.credentials.get("phone", "")

            # Check if we've ever seen this account.
            # Simple heuristic: if the account's phone appears in any telegram_users.phone,
            # we've processed them before. Otherwise, new account.
            seen_before = False
            if phone:
                async with self.pool.acquire() as conn:
                    row = await conn.fetchval(
                        "SELECT 1 FROM telegram_users WHERE phone = $1 LIMIT 1",
                        phone,
                    )
                    seen_before = row is not None

            if seen_before:
                logger.debug(
                    "[worker=%d account=%s] Account already known — skipping auto-backfill",
                    worker.worker_id, account_name,
                )
                continue

            logger.info(
                "[worker=%d account=%s] NEW account detected — running full dialog discovery",
                worker.worker_id, account_name,
            )

            # Enumerate all dialogs this account is in.
            dialogs = await self.collect_dialogs()

            # Each dialog was upserted into telegram_chats by collect_dialogs().
            # P3-2: bounded enqueue with backpressure (was an unbounded loop).
            enqueued = await self._spider_enqueue(dialogs, "auto_backfill", priority=8)

            logger.info(
                "[worker=%d account=%s] Enqueued %d/%d chat(s) for auto-backfill",
                worker.worker_id, account_name, enqueued, len(dialogs),
            )

            # Record the account's own user profile.
            try:
                me = await worker.client.get_me()
                await self._upsert_user_full(me)
            except Exception as exc:
                logger.debug("get_me() failed for account=%s: %s", account_name, exc)

    async def _listen_for_new_accounts(self) -> None:
        """Listen for pg_notify('telegram_account_added', name) and hot-reload (item 4.6).

        This runs as a background task during the collection cycle. When a new
        account is onboarded via bot or dashboard, we receive the notification,
        load the account from DB, spawn a new worker, and trigger backfill.
        """
        if self.pool is None:
            return

        try:
            conn = await self.pool.acquire()
            await conn.add_listener("telegram_account_added", self._on_account_added_notify)
            logger.info("Listening for telegram_account_added notifications")

            # Keep connection alive while listening.
            while not self._stop.is_set():
                await asyncio.sleep(5)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("_listen_for_new_accounts failed: %s", exc)
        finally:
            try:
                await conn.remove_listener("telegram_account_added", self._on_account_added_notify)
                await self.pool.release(conn)
            except Exception:
                pass

    def _on_account_added_notify(self, conn, pid, channel, payload: str) -> None:
        """Handle pg_notify callback for new account."""
        logger.info("Received telegram_account_added notification: %s", payload)
        # Schedule the async handler — can't await directly from callback.
        asyncio.create_task(self._handle_new_account(payload))

    async def _handle_new_account(self, account_name: str) -> None:
        """Load and connect a newly-onboarded account, then trigger backfill."""
        if self.pool is None:
            return

        # Load account from DB.
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT name, api_id, api_hash, phone, session_string
                FROM telegram_user_accounts
                WHERE name = $1 AND status = 'active'
                """,
                account_name,
            )

        if not row:
            logger.warning("Account %s not found or inactive — skipping hot-reload", account_name)
            return

        # Check if already loaded.
        existing = [a for a in self.account_pool._accounts if a.name == row["name"]]
        if existing:
            logger.info("Account %s already loaded — skipping", account_name)
            return

        # Add to pool.
        self.account_pool.add_account(
            name=row["name"],
            credentials={
                "api_id": str(row["api_id"]),
                "api_hash": row["api_hash"],
                "phone": row["phone"],
                "session": row["session_string"],
            },
        )
        logger.info("Hot-loaded account %s from DB", account_name)

        # Spawn a new worker for this account.
        from src.collectors.telegram import TelegramWorker
        new_account = self.account_pool._accounts[-1]  # Just added
        worker = TelegramWorker(self, new_account, len(self._workers))
        try:
            await worker.connect()
            self._workers.append(worker)
            logger.info("[worker=%d account=%s] Hot-connected new worker", worker.worker_id, account_name)
            if os.getenv("TELEGRAM_REALTIME_ENABLED", "true").lower() == "true":
                self._register_realtime_handlers_for_worker(worker)

            # Trigger full dialog discovery + backfill for the new account.
            dialogs = await self.collect_dialogs()
            # P3-2: bounded enqueue with backpressure (was an unbounded loop).
            enqueued = await self._spider_enqueue(dialogs, "hot_reload", priority=9)
            logger.info("[worker=%d] Enqueued %d/%d chats for hot-reload backfill", worker.worker_id, enqueued, len(dialogs))

        except Exception as exc:
            logger.error("Failed to hot-connect account %s: %s", account_name, exc)

    def _dispatch(self, targets: list[str], num_workers: int) -> list[list[str]]:
        """Hash-bucket targets so each chat is owned by exactly one worker.

        Even if multiple accounts are members of the same chat, only one
        worker (one account) will scrape it per cycle — shared-channel dedup.
        """
        buckets: list[list[str]] = [[] for _ in range(num_workers)]
        for t in targets:
            idx = (hash(t) & 0x7FFFFFFF) % num_workers
            buckets[idx].append(t)
        for i, b in enumerate(buckets):
            logger.info("[dispatch] worker=%d -> %d target(s)", i, len(b))
        return buckets

    # ------------------------------------------------------------------
    # Top-level collect (parallel)
    # ------------------------------------------------------------------

    async def collect(self, targets: list[str]):
        # Longer delay to let other collectors (instagram/tiktok) finish their
        # SYNC initialization that blocks the event loop. Without this, our
        # async connects never complete because instaloader's retry loops freeze
        # the entire asyncio event loop.
        logger.info("[telegram.collect] waiting 30s for other collectors to settle...")
        await asyncio.sleep(30)

        # Rate-limit full reconnect cycles — Telegram revokes auth keys when
        # all accounts reconnect rapidly in a short window (e.g. container restarts).
        # Enforce minimum 5 minutes between full connection attempts.
        now = asyncio.get_event_loop().time()
        last = getattr(self, "_last_connect_time", 0)
        min_interval = 300.0  # 5 minutes
        if now - last < min_interval:
            wait = min_interval - (now - last)
            logger.info("[telegram.collect] rate-limiting reconnect — waiting %.0fs", wait)
            await asyncio.sleep(wait)
        self._last_connect_time = asyncio.get_event_loop().time()

        logger.info("[telegram.collect] ENTER with %d targets", len(targets))
        if not self._api_id or not self._api_hash:
            logger.error("TELEGRAM_API_ID and TELEGRAM_API_HASH required")
            return

        # Load accounts from DB (supplements env-based accounts) — item 4.5
        logger.info("[telegram.collect] calling _load_accounts_from_db")
        await self._load_accounts_from_db()

        logger.info("[telegram.collect] calling _spawn_workers")
        self._workers = await self._spawn_workers()
        if not self._workers:
            logger.error("No Telegram workers connected — aborting cycle")
            return
        logger.info("[telegram.collect] got %d workers", len(self._workers))

        # Register realtime handlers EARLY — telethon fires them from each client's own
        # update loop the moment they're attached, so LIVE messages stream in while the
        # heavy historical backfill below runs. (Previously realtime started only AFTER
        # backfill, so live capture stalled for hours behind a multi-thousand-chat backfill.)
        if os.getenv("TELEGRAM_REALTIME_ENABLED", "true").lower() == "true":
            try:
                await self._register_realtime_handlers()
            except Exception as e:
                logger.error("early realtime handler registration failed: %s", e)

        # Auto-backfill new accounts (item 2.4).
        # For each connected worker, check if their account name has been seen
        # before. If not, run full collect_dialogs() to discover all their chats
        # and queue them for backfill.
        # Keep startup media/realtime-first. This path can be expensive because
        # collect_dialogs() iterates every connected worker; a weak "new account"
        # heuristic previously re-ran it for old accounts after each restart and
        # destabilized MTProto sessions. Hot-added accounts still use the explicit
        # _handle_new_account backfill path below.
        auto_backfill_enabled = (
            os.getenv("TELEGRAM_AUTO_BACKFILL_NEW_ACCOUNTS", "false").lower() == "true"
        )
        if auto_backfill_enabled and self.pool is not None:
            logger.info("[telegram.collect] starting _auto_backfill_new_accounts")
            await self._auto_backfill_new_accounts()
            logger.info("[telegram.collect] finished _auto_backfill_new_accounts")

        # HubNotifier + BotPool keyed off the primary client (first worker).
        self._hub_notifier.set_client(self._primary_client)
        await self._hub_notifier.start()
        await self._bot_pool.start_health_monitor()
        logger.info("[telegram.collect] HubNotifier + BotPool started")

        # Start hot-reload listener for new accounts (item 4.6).
        # This listens for pg_notify('telegram_account_added', name) and spawns
        # a new worker when an account is onboarded via bot or dashboard.
        self._hot_reload_task = asyncio.create_task(self._listen_for_new_accounts())
        logger.info("[telegram.collect] hot_reload_task started")

        self._hub_notifier.notify(
            NotifyCategory.COLLECTION_START,
            f"Starting collection of {len(targets)} targets across {len(self._workers)} accounts",
        )

        # Dispatch targets to workers (hash-based, shared-chat aware).
        buckets = self._dispatch(targets, len(self._workers))
        for w, bucket in zip(self._workers, buckets):
            w._claimed_chats = set(bucket)

        # Run all workers concurrently — true parallelism.
        results = await asyncio.gather(
            *(w.run_targets(bucket) for w, bucket in zip(self._workers, buckets)),
            return_exceptions=True,
        )
        for w, r in zip(self._workers, results):
            if isinstance(r, Exception):
                logger.error(
                    "[worker=%d account=%s] worker crashed: %s",
                    w.worker_id, w.account.name, r,
                )

        # Launch the resolve-only sweep as an INDEPENDENT task now that workers are
        # connected — BEFORE the blocking spider drain below (and collect_realtime),
        # which can sit for hours deep-backfilling large channels. As its own task it
        # runs on the event loop concurrently with those blocked gathers, so dead
        # chats get reclassified promptly instead of waiting for a worker to free up.
        if self._workers and not getattr(self, "_sweep_task", None):
            self._sweep_task = asyncio.create_task(self._resolve_sweep_loop())

        # Spider queue: fan out across allowed workers for parallelism.
        # TELEGRAM_SPIDER_ACCOUNTS restricts which accounts can spider.
        if os.getenv("TELEGRAM_SPIDER_ENABLED", "true").lower() == "true":
            try:
                spider_workers = [
                    w for w in self._workers
                    if self._is_spider_allowed(w)
                ]
                if self._spider_accounts and not spider_workers:
                    logger.warning(
                        "Spider enabled but no connected workers match TELEGRAM_SPIDER_ACCOUNTS=%s; skipping spider queue",
                        ",".join(sorted(self._spider_accounts)),
                    )
                elif self._spider_accounts and len(spider_workers) < len(self._workers):
                    logger.info("Spider restricted to accounts: %s (%d/%d workers)",
                                ", ".join(sorted(self._spider_accounts)), len(spider_workers), len(self._workers))
                if spider_workers:
                    spider_tasks = [
                        self._process_spider_queue(w)
                        for w in spider_workers
                    ]
                    results = await asyncio.gather(*spider_tasks, return_exceptions=True)
                    for w, r in zip(spider_workers, results):
                        if isinstance(r, Exception):
                            logger.error("Spider queue worker=%d crashed: %s", w.worker_id, r)
            except Exception as e:
                logger.error("Spider queue processing failed: %s", e)

        if self._story_enabled:
            try:
                await self._scan_stories(self._workers[0], targets)
            except Exception as e:
                logger.error("Story scan failed: %s", e)

        # Tier 5: drain shared/live-location coords from message metadata into
        # telegram_message_locations (bounded per-cycle; reads existing data).
        if os.getenv("TELEGRAM_LOCATION_BACKFILL_ENABLED", "true").lower() == "true":
            try:
                await self._backfill_message_locations(
                    int(os.getenv("TELEGRAM_LOCATION_BACKFILL_BATCH", "500")))
            except Exception as e:
                logger.debug("location backfill failed: %s", e)

        if self._group_join_enabled:
            try:
                await self._process_join_queue()
            except Exception as e:
                logger.error("Join queue failed: %s", e)

        # ── REALTIME LISTENER (the missing wire) ────────────────────────────
        # collect_realtime() was defined but NEVER called, so telegram only ever
        # ran the one-shot backfill/spider pass above and caught NO new messages
        # (root cause of the multi-day message gap). Registering the @client.on
        # NewMessage handlers + parking here makes collect() BLOCK as a true
        # realtime source (same model as whatsapp). Backfill/spider above run
        # once per (re)launch; from here the telethon update loop owns the
        # session uncontended, so get_difference can catch up the gap and live
        # messages stream in. Gated so it can be disabled if ever needed.
        if os.getenv("TELEGRAM_REALTIME_ENABLED", "true").lower() == "true":
            logger.info("[telegram.collect] entering realtime listener (parks until stop)")
            await self.collect_realtime()

    async def _spider_enqueue(self, rows, source_tag: str, priority: int = 8) -> int:
        """P3-2: bounded enqueue into telegram_spider_queue with backpressure.

        Unbounded auto_backfill enqueue (1500+ pending) vs a single-worker drain
        cadence produced an ever-growing backlog that starved live collection.
        This caps the pending depth at TELEGRAM_SPIDER_QUEUE_MAX (default 2000):
        once the queue is at/over the cap we stop enqueueing new discovery work
        so collection of already-queued + realtime chats can catch up. ON CONFLICT
        DO NOTHING keeps it idempotent. Returns the number actually enqueued.
        """
        if self.pool is None:
            return 0
        cap = int(os.getenv("TELEGRAM_SPIDER_QUEUE_MAX", "2000"))
        async with self.pool.acquire() as conn:
            pending = await conn.fetchval(
                "SELECT COUNT(*) FROM telegram_spider_queue WHERE status = 'pending'"
            )
        budget = cap - int(pending or 0)
        if budget <= 0:
            logger.warning(
                "spider queue at cap (%d/%d pending) - skipping %s enqueue of %d "
                "chats (backpressure)", pending, cap, source_tag, len(rows),
            )
            return 0
        enqueued = 0
        for d in rows:
            if enqueued >= budget:
                logger.warning(
                    "spider queue cap reached mid-enqueue (%s); deferred %d chats",
                    source_tag, len(rows) - enqueued,
                )
                break
            pcid = d.get("platform_chat_id")
            if not pcid:
                continue
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO telegram_spider_queue
                        (platform_chat_id, title, source, priority, status, collected_at)
                    VALUES ($1, $2, $3, $4, 'pending', NOW())
                    ON CONFLICT (platform_chat_id) DO NOTHING
                    """,
                    pcid, d.get("title"), source_tag, priority,
                )
            enqueued += 1
        return enqueued

    async def _process_spider_queue(self, worker: "TelegramWorker"):
        """Process telegram_spider_queue jobs using the given worker's client."""
        from telethon.errors import FloodWaitError
        max_per_cycle = int(os.getenv("TELEGRAM_SPIDER_MAX_PER_CYCLE", "50"))
        await refresh_account_proximity_cache(self.pool)
        processed = 0
        while not self._stop.is_set() and processed < max_per_cycle:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    WITH ranked AS (
                        SELECT q.id,
                               q.priority,
                               q.collected_at,
                               prox.proximity_tier
                        FROM telegram_spider_queue q
                        LEFT JOIN LATERAL (
                            SELECT MIN(ap.tier) AS proximity_tier
                            FROM telegram_chats c
                            JOIN telegram_chat_members m ON m.chat_id = c.id
                            JOIN telegram_users u ON u.id = m.user_id
                            JOIN account_proximity_cache ap
                              ON ap.platform = 'telegram'
                             AND ap.account_id = u.platform_user_id
                            WHERE c.platform_chat_id = q.platform_chat_id
                        ) prox ON TRUE
                        WHERE q.status = 'pending'
                        ORDER BY
                            CASE
                                WHEN prox.proximity_tier IN (1, 2) THEN 2
                                WHEN prox.proximity_tier = 3 THEN 1
                                ELSE 0
                            END DESC,
                            q.priority ASC,
                            q.collected_at ASC
                        LIMIT 20
                    ),
                    candidate AS (
                        SELECT q.id
                        FROM telegram_spider_queue q
                        JOIN ranked r ON r.id = q.id
                        WHERE q.status = 'pending'
                        ORDER BY
                            CASE
                                WHEN r.proximity_tier IN (1, 2) THEN 2
                                WHEN r.proximity_tier = 3 THEN 1
                                ELSE 0
                            END DESC,
                            r.priority ASC,
                            r.collected_at ASC
                        LIMIT 1
                        FOR UPDATE OF q SKIP LOCKED
                    )
                    UPDATE telegram_spider_queue
                    SET status = 'processing'
                    WHERE id = (SELECT id FROM candidate)
                      AND status = 'pending'
                    RETURNING platform_chat_id, title
                """)
            if not row:
                break
            chat_id = row['platform_chat_id']
            title = row['title'] or chat_id
            try:
                logger.info(
                    "[spider w=%d] processing chat %s (%s) [%d/%d]",
                    worker.worker_id, chat_id, title, processed + 1, max_per_cycle,
                )
                await self._collect_chat(worker, chat_id)
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE telegram_spider_queue SET status = 'completed' WHERE platform_chat_id = $1",
                        chat_id,
                    )
                processed += 1
            except FloodWaitError as e:
                logger.warning(
                    "[spider w=%d] FloodWait %ds on chat %s — releasing back to pending",
                    worker.worker_id, e.seconds, chat_id,
                )
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE telegram_spider_queue SET status = 'pending' WHERE platform_chat_id = $1",
                        chat_id,
                    )
                await self._handle_flood_wait(worker, e)
            except EntityUnresolvable as exc:
                # Every account was asked and none owns this chat (left / deleted /
                # private). Terminal — mark 'unresolvable' so it stops churning the
                # queue, distinct from a retryable 'failed'.
                logger.info(
                    "[spider w=%d] chat %s unresolvable by all %d accounts: %s",
                    worker.worker_id, chat_id, len(self._workers), exc,
                )
                await self._mark_spider_status(chat_id, "unresolvable", str(exc))
            except Exception as exc:
                # Transient (disconnect/timeout) or unknown — retry up to a cap so a
                # blip doesn't permanently kill a recoverable chat; then give up.
                await self._retry_or_fail_spider(chat_id, exc, worker.worker_id)
        logger.info("[spider w=%d] finished: processed %d chats", worker.worker_id, processed)

    async def _mark_spider_status(self, chat_id: str, status: str, err: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE telegram_spider_queue SET status = $2, last_error = $3 "
                "WHERE platform_chat_id = $1",
                chat_id, status, (err or "")[:500],
            )

    async def _retry_or_fail_spider(self, chat_id: str, exc: Exception, worker_id: int):
        """Increment attempts; re-queue as 'pending' until the cap, then 'failed'."""
        max_attempts = int(os.getenv("TELEGRAM_SPIDER_MAX_ATTEMPTS", "5"))
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE telegram_spider_queue "
                "SET attempts = COALESCE(attempts, 0) + 1, last_error = $2, "
                "    status = CASE WHEN COALESCE(attempts, 0) + 1 >= $3 THEN 'failed' "
                "                  ELSE 'pending' END "
                "WHERE platform_chat_id = $1 RETURNING attempts, status",
                chat_id, f"{type(exc).__name__}: {exc}"[:500], max_attempts,
            )
        attempts = row["attempts"] if row else 0
        if row and row["status"] == "failed":
            logger.warning(
                "[spider w=%d] chat %s failed after %d attempts: %s",
                worker_id, chat_id, attempts, exc,
            )
        else:
            logger.info(
                "[spider w=%d] chat %s transient (attempt %d/%d) — re-queued: %s",
                worker_id, chat_id, attempts, max_attempts, exc,
            )

    # ------------------------------------------------------------------
    # Resolve-only sweep: fast dead-chat cleanup, independent of the drain
    # ------------------------------------------------------------------

    async def _sweep_resolve_pending(self) -> dict:
        """Cheaply resolve pending chats against ALL accounts and mark the truly
        unresolvable ones terminal WITHOUT a full backfill.

        Rationale: the drain deep-backfills one chat per worker at a time, so a few
        huge channels monopolize all workers and dead chats (left/deleted/private)
        sit behind them un-reclassified. This pass only calls get_entity (no message
        pull), so it clears the dead ones in seconds each. Resolvable chats are left
        'pending' (marked resolve_checked_at) for the drain to backfill. Transient
        failures are left unchecked so the next sweep retries them.
        """
        batch = int(os.getenv("TELEGRAM_RESOLVE_SWEEP_BATCH", "400"))
        out = {"checked": 0, "resolvable": 0, "unresolvable": 0, "transient": 0}
        if not self._workers:
            return out
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT platform_chat_id FROM telegram_spider_queue "
                "WHERE status = 'pending' AND resolve_checked_at IS NULL "
                "ORDER BY priority ASC, collected_at ASC LIMIT $1",
                batch,
            )
        if not rows:
            return out
        for r in rows:
            if self._stop.is_set():
                break
            chat_id = r["platform_chat_id"]
            verdict = await self._resolves_on_any_worker(chat_id)
            if verdict == "ok":
                # Resolvable — mark checked, leave 'pending' for the drain.
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE telegram_spider_queue SET resolve_checked_at = now() "
                        "WHERE platform_chat_id = $1 AND status = 'pending'",
                        chat_id,
                    )
                out["resolvable"] += 1
            elif verdict == "dead":
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE telegram_spider_queue SET status = 'unresolvable', "
                        "resolve_checked_at = now(), "
                        "last_error = 'resolve-sweep: no account owns chat' "
                        "WHERE platform_chat_id = $1 AND status = 'pending'",
                        chat_id,
                    )
                out["unresolvable"] += 1
            else:  # transient — leave unchecked, retried next sweep
                out["transient"] += 1
            out["checked"] += 1
        logger.info(
            "[resolve-sweep] checked=%d resolvable=%d unresolvable=%d transient=%d",
            out["checked"], out["resolvable"], out["unresolvable"], out["transient"],
        )
        return out

    async def _resolve_sweep_loop(self):
        """Run the resolve sweep on its own cadence, independent of the drain gather
        (which can be blocked for hours deep-backfilling a large channel)."""
        if os.getenv("TELEGRAM_RESOLVE_SWEEP_ENABLED", "1").lower() != "1":
            return
        interval = float(os.getenv("TELEGRAM_RESOLVE_SWEEP_INTERVAL", "120"))
        logger.info("[resolve-sweep] loop started (interval=%.0fs)", interval)
        while not self._stop.is_set():
            try:
                res = await self._sweep_resolve_pending()
                if res["checked"] == 0:
                    # Nothing unchecked left — idle longer until new chats enqueue.
                    await asyncio.sleep(interval * 5)
                    continue
            except Exception as exc:
                logger.debug("[resolve-sweep] loop error: %s", exc)
            await asyncio.sleep(interval)

    async def _handle_flood_wait(self, worker: "TelegramWorker", error):
        wait_seconds = getattr(error, "seconds", 60)
        worker.state = SessionState.FLOOD_WAIT
        logger.warning(
            "[worker=%d account=%s] FloodWait: sleeping %ds",
            worker.worker_id, worker.account.name, wait_seconds,
        )
        # record_flood_wait classifies the error AND sets cooldown.
        # Use the actual flood-wait seconds so the pool doesn't release the
        # account until Telegram lets us back in.
        self.account_pool.record_flood_wait(worker.account.name, float(wait_seconds))
        await record_rate_limit_event(
            self.pool,
            source="telegram",
            account=worker.account.name,
            scope="flood_wait",
            status_code=429,
            cooldown_seconds=int(wait_seconds),
            reason="Telegram FloodWaitError",
            metadata={
                "worker_id": worker.worker_id,
                "exception": type(error).__name__,
                "wait_seconds": wait_seconds,
            },
        )
        # Sleep at least until the flood-wait elapses (capped to 5min so we
        # don't block the worker on truly long bans — those are surfaced by
        # is_available() and the next cycle skips this acct).
        await asyncio.sleep(min(wait_seconds, 300))
        worker.state = SessionState.CONNECTED

    # ------------------------------------------------------------------
    # Per-chat collection (now takes a worker arg)
    # ------------------------------------------------------------------

    async def _resolve_entity_any_worker(self, preferred: "TelegramWorker", target: str):
        """Resolve a chat entity using whichever account is actually IN it.

        Each chat lives in exactly one of the N accounts. The spider queue hands a
        chat to whatever worker is free, so ~ (N-1)/N of the time the processing
        account isn't a member and Telethon raises "Cannot find any entity" — that
        was the real cause of the ~2k 'failed' backfills, not the sessions. Try the
        preferred worker first, then the others, and return (worker, entity) for the
        one that owns the chat so the backfill runs on the right account.
        """
        order = [preferred] + [w for w in self._workers if w is not preferred]
        last_exc = None
        transient = False  # True if ANY account failed for a retryable reason
        # Bound EACH lookup: a half-dead connection (we see "Server closed the
        # connection" blips) must not wedge the whole backfill drain. On timeout we
        # just move to the next account.
        timeout = float(os.getenv("TELEGRAM_RESOLVE_TIMEOUT", "25"))
        for w in order:
            # A disconnected account can't answer now but MIGHT own the chat — treat
            # its unavailability as transient so we retry, not mark unresolvable.
            if getattr(w, "state", None) not in (None, SessionState.CONNECTED):
                transient = True
                continue
            # Try the id numerically then as a raw string; get_entity raises
            # ValueError('Cannot find any entity...') when this account isn't in it.
            for form in (_as_int(target), target):
                if form is None:
                    continue
                try:
                    return w, await asyncio.wait_for(w.client.get_entity(form), timeout)
                except (ValueError, TypeError) as ve:
                    last_exc = ve  # entity-not-found on this account — not transient
                    continue
                except Exception as exc:
                    last_exc = exc
                    if _is_transient(exc):
                        transient = True
                    break  # move to the next account
        # Every account was tried. If any failure was transient, surface it so the
        # caller retries; only when ALL accounts cleanly said "not found" is the
        # chat genuinely unresolvable (left/deleted/private).
        if transient:
            raise last_exc or asyncio.TimeoutError(f"transient resolve failure for {target!r}")
        raise EntityUnresolvable(f"no connected account owns entity {target!r}")

    async def _resolves_on_any_worker(self, target: str) -> str:
        """Fast, LOCAL resolvability check for the resolve sweep.

        Uses get_input_entity (session-cache lookup, populated by dialog discovery)
        instead of get_entity (a network call that queues behind each busy worker's
        backfill iter_messages). For a chat an account is in this hits the cache and
        returns instantly; for one it isn't in, get_input_entity raises ValueError
        LOCALLY (no network, no FloodWait risk). Returns 'ok' | 'dead' | 'transient'.
        """
        cid = _as_int(target)
        transient = False
        for w in self._workers:
            if getattr(w, "state", None) not in (None, SessionState.CONNECTED):
                transient = True  # a disconnected account might own it — retry later
                continue
            try:
                await w.client.get_input_entity(cid if cid is not None else target)
                return "ok"
            except (ValueError, TypeError):
                continue  # this account doesn't know the chat — try the next
            except Exception as exc:
                transient = True
                if _is_flood_wait(exc):
                    # Shouldn't happen for a cache lookup, but be safe.
                    logger.debug("[resolve-sweep] FloodWait on get_input_entity")
                continue
        return "transient" if transient else "dead"

    async def _collect_chat(self, worker: "TelegramWorker", target: str):
        from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

        # Use whichever account is actually in this chat (cross-account routing).
        worker, entity = await self._resolve_entity_any_worker(worker, target)
        client = worker.client

        chat_id = str(entity.id)
        chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or chat_id
        chat_username = getattr(entity, "username", None)  # for source_url deep-links

        await self._upsert_chat(entity)
        await self._collect_profile_photo(worker, entity, chat_id, chat_name)

        last_id = self.checkpoint.last_processed_id
        try:
            min_id = int(last_id) if last_id else 0
        except (TypeError, ValueError):
            min_id = 0
        count = 0

        async for message in client.iter_messages(entity, min_id=min_id, limit=None):
            if self._stop.is_set():
                break

            await self.wait_rate_limit("telegram.org")

            sender_uuid = None
            if message.sender_id:
                sender_uuid = await self._upsert_sender(worker, message.sender_id)

            await self._upsert_message(message, chat_id, sender_uuid, worker=worker)

            # Forward extraction → spider queue (item 1.10).
            # When a message is forwarded from another chat or user, that source
            # is a discovery edge: we want to enqueue it for spidering.
            try:
                await self._enqueue_forward_edges(message, parent_chat_platform_id=chat_id)
            except Exception as exc:
                logger.debug("_enqueue_forward_edges failed: %s", exc)

            # Reactor enumeration (item 2.2) — enumerate who reacted and enqueue
            # them as spider seeds. Called during backfill for every message with
            # reactions. Expensive (1 API call per emoji), so rate-limited.
            if getattr(message, "reactions", None) is not None:
                try:
                    await self._enumerate_reactors_and_enqueue(worker, message, chat_id)
                except Exception as exc:
                    logger.debug("_enumerate_reactors_and_enqueue failed: %s", exc)

            if message.media:
                if isinstance(message.media, MessageMediaPhoto):
                    await self._handle_photo(worker, message, chat_id, chat_name, chat_username)
                    count += 1
                elif isinstance(message.media, MessageMediaDocument):
                    doc = message.media.document
                    if doc and (getattr(doc, "size", 0) or 0) <= self._max_media_size:
                        mime = getattr(doc, "mime_type", "") or ""
                        # Tier 3: route ALL documents through the classifier
                        # (was image/video-only, which dropped PDFs/office/audio).
                        # The classifier whitelists safe docs + audio + static
                        # stickers and skips executables/code/animated stickers.
                        if await self._handle_document(worker, message, chat_id, chat_name, mime, chat_username):
                            count += 1

            if count % self._batch_size == 0 and count > 0:
                await self.checkpoint.save_progress(str(message.id))

        # P3-6: flush the final partial batch. The per-batch save above only
        # fires on exact _batch_size multiples, so the trailing remainder
        # (up to _batch_size-1 messages) was never checkpointed — a SIGTERM or
        # normal completion mid-remainder lost that cursor progress and forced
        # re-collection on restart. Persist the last seen id unconditionally.
        if count > 0:
            try:
                await self.checkpoint.save_progress(str(message.id))
            except Exception:
                logger.warning("telegram/%s: final checkpoint flush failed",
                               chat_name, exc_info=True)

        if count > 0:
            logger.info(
                "[worker=%d account=%s] telegram/%s: finished processing %d media items",
                worker.worker_id, worker.account.name, chat_name, count,
            )

        # Per-chat membership snapshot (item 1.9) — only meaningful for groups
        # where we have visibility. Channels with broadcast=True don't expose
        # iter_participants for non-admins; the call will return 0 silently.
        if getattr(entity, "megagroup", False) or not getattr(entity, "broadcast", False):
            try:
                await self.collect_chat_members(entity.id, worker=worker)
            except Exception as exc:
                logger.debug(
                    "collect_chat_members deferred-call failed for chat=%s: %s",
                    chat_id, exc,
                )

        # Discussion group spider (item 2.1) — channels may have a linked
        # discussion group. If so, we join, scrape members+messages, leave.
        if getattr(entity, "broadcast", False):
            if not self._is_spider_allowed(worker):
                logger.debug(
                    "[worker=%d account=%s] skipping discussion spider for channel=%s; account not in TELEGRAM_SPIDER_ACCOUNTS",
                    worker.worker_id, worker.account.name, chat_id,
                )
            else:
                try:
                    await self._spider_discussion_group(worker, entity, chat_id)
                except Exception as exc:
                    logger.debug(
                        "_spider_discussion_group failed for channel=%s: %s",
                        chat_id, exc,
                    )

        # Mark this target as collected in collection_targets
        logger.info("[_collect_chat] completed target=%s, calling mark_target_collected", target)
        await self.mark_target_collected(target)

    async def _spider_discussion_group(
        self, worker: "TelegramWorker", channel_entity, channel_platform_id: str
    ) -> None:
        """Spider the discussion group linked to a channel (item 2.1).

        Flow:
          1. Check if channel has `linked_chat_id`; if not, return early.
          2. Check if we've already visited this discussion group recently
             (within 24h) — if so, skip to avoid churn.
          3. If not already joined, call `JoinChannelRequest(linked_chat)`.
          4. Wait a random human-like dwell time (60-180s default).
          5. Run `collect_chat_members()` + read discussion messages. Set
             TELEGRAM_DISCUSSION_MESSAGE_LIMIT=0 for all available history.
          6. Call `LeaveChannelRequest` only if this pass joined the discussion.
          7. Record the visit in `telegram_discussion_visits`.
        """
        import random
        from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest

        linked_chat_id = getattr(channel_entity, "linked_chat_id", None)
        if not linked_chat_id:
            return

        client = worker.client
        discussion_platform_id = str(linked_chat_id)

        # Resolve channel UUID for FK.
        async with self.pool.acquire() as conn:
            channel_row = await conn.fetchrow(
                "SELECT id FROM telegram_chats WHERE platform_chat_id = $1",
                channel_platform_id,
            )
            if channel_row is None:
                return
            channel_uuid = channel_row["id"]

            # Check recent visits — skip if visited within 24h.
            recent = await conn.fetchval(
                """
                SELECT 1 FROM telegram_discussion_visits
                WHERE channel_chat_id = $1 AND joined_at > NOW() - INTERVAL '24 hours'
                LIMIT 1
                """,
                channel_uuid,
            )
            if recent:
                logger.debug(
                    "Skipping discussion spider for channel=%s — visited <24h ago",
                    channel_platform_id,
                )
                return

        # Get discussion group entity.
        try:
            discussion_entity = await client.get_entity(int(linked_chat_id))
        except Exception as exc:
            logger.debug("Cannot resolve discussion group %s: %s", linked_chat_id, exc)
            return

        await self._upsert_chat(discussion_entity)
        discussion_title = (
            getattr(discussion_entity, "title", None)
            or getattr(discussion_entity, "username", None)
            or discussion_platform_id
        )

        # Resolve discussion UUID.
        async with self.pool.acquire() as conn:
            disc_row = await conn.fetchrow(
                "SELECT id FROM telegram_chats WHERE platform_chat_id = $1",
                discussion_platform_id,
            )
            discussion_uuid = disc_row["id"] if disc_row else None

        # Check if we're already a member; if not, join.
        already_member = False
        joined_this_pass = False
        try:
            # `get_participants` returns an empty iterator if not a member (for
            # supergroups you can't read). Alternatively, check left/banned flags.
            me = await client.get_me()
            me_participant = await client.get_permissions(discussion_entity, me)
            already_member = not getattr(me_participant, "left", True)
        except Exception:
            pass

        abort_reason: str | None = None
        members_collected = 0
        messages_collected = 0

        try:
            if not already_member:
                logger.info(
                    "[worker=%d] Joining discussion group %s (%s) for channel %s",
                    worker.worker_id, discussion_title, discussion_platform_id, channel_platform_id,
                )
                await client(JoinChannelRequest(discussion_entity))
                joined_this_pass = True

                # Human-like dwell before scraping.
                dwell = random.randint(self._discussion_dwell_min, self._discussion_dwell_max)
                logger.debug("Dwelling %ds before scraping discussion group", dwell)
                await asyncio.sleep(dwell)

            # Scrape members.
            members_collected = await self.collect_chat_members(
                discussion_entity.id, worker=worker
            )

            # Scrape discussion messages. None means all available history.
            msg_count = 0
            async for message in client.iter_messages(
                discussion_entity,
                limit=self._discussion_message_limit,
            ):
                if self._stop.is_set():
                    abort_reason = "stop_signal"
                    break
                sender_uuid = None
                if message.sender_id:
                    sender_uuid = await self._upsert_sender(worker, message.sender_id)
                await self._upsert_message(message, discussion_platform_id, sender_uuid, worker=worker)
                try:
                    await self._enqueue_forward_edges(message, discussion_platform_id)
                except Exception:
                    pass
                msg_count += 1
            messages_collected = msg_count

        except Exception as exc:
            if _is_flood_wait(exc):
                await self._handle_flood_wait(worker, exc)
                abort_reason = "flood_wait"
            else:
                logger.error("Discussion spider failed for %s: %s", discussion_platform_id, exc)
                abort_reason = str(type(exc).__name__)[:64]

        finally:
            left_at = None
            if joined_this_pass:
                try:
                    logger.info(
                        "[worker=%d] Leaving discussion group %s",
                        worker.worker_id, discussion_title,
                    )
                    await client(LeaveChannelRequest(discussion_entity))
                    left_at = datetime.now(timezone.utc)
                except Exception as leave_exc:
                    logger.debug("LeaveChannelRequest failed: %s", leave_exc)
            else:
                logger.debug(
                    "[worker=%d] Keeping existing discussion membership for %s",
                    worker.worker_id, discussion_title,
                )

            # Record the visit.
            if discussion_uuid is not None:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO telegram_discussion_visits (
                            channel_chat_id, discussion_chat_id,
                            account_name, joined_at, left_at,
                            members_collected, messages_collected, abort_reason
                        ) VALUES ($1, $2, $3, NOW(), $4, $5, $6, $7)
                        """,
                        channel_uuid,
                        discussion_uuid,
                        worker.account.name,
                        left_at,
                        members_collected,
                        messages_collected,
                        abort_reason,
                    )

        logger.info(
            "[worker=%d] Discussion spider done: channel=%s discussion=%s members=%d msgs=%d limit=%s joined=%s left=%s abort=%s",
            worker.worker_id, channel_platform_id, discussion_platform_id,
            members_collected, messages_collected,
            "all" if self._discussion_message_limit is None else self._discussion_message_limit,
            joined_this_pass, left_at is not None, abort_reason,
        )

    async def _upsert_chat(self, entity):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO telegram_chats (platform_chat_id, title, username, type, description, members_count, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (platform_chat_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    username = EXCLUDED.username,
                    type = EXCLUDED.type,
                    description = EXCLUDED.description,
                    members_count = COALESCE(EXCLUDED.members_count, telegram_chats.members_count),
                    updated_at = NOW()
            """,
            str(entity.id),
            getattr(entity, 'title', None),
            getattr(entity, 'username', None),
            'channel' if getattr(entity, 'broadcast', False) else 'group',
            getattr(entity, 'about', None),
            getattr(entity, 'participants_count', None)
            )
        self._archive_raw_payload(
            artifact_id=f"chats/{getattr(entity, 'id', 'unknown')}",
            payload=_telethon_payload(entity),
            target_tables=["telegram_chats"],
            metadata={
                "platform_chat_id": str(getattr(entity, "id", "")),
                "username": getattr(entity, "username", None),
                "ingest_path": self.INGEST_PATH,
                "raw_payload_kind": "chat",
            },
        )

    def _archive_raw_payload(
        self,
        *,
        artifact_id: str,
        payload: dict,
        target_tables: list[str],
        metadata: dict | None = None,
    ) -> None:
        if not _tier1_raw_archives_enabled():
            return
        try:
            result = write_raw_payload(
                source=self.SOURCE_NAME,
                artifact_id=artifact_id,
                payload=payload,
                metadata=metadata or {},
                target_tables=target_tables,
                root=VAULT_ROOT,
            )
            report_raw_archive_result(
                self.pool,
                source=self.SOURCE_NAME,
                artifact_id=artifact_id,
                result=result,
                metadata=metadata,
                log=logger,
            )
        except Exception as exc:
            logger.debug("telegram raw archive failed for %s: %s", artifact_id, exc)
            report_raw_archive_result(
                self.pool,
                source=self.SOURCE_NAME,
                artifact_id=artifact_id,
                result=None,
                metadata=metadata,
                log=logger,
                error=str(exc),
            )

    async def _enqueue_forward_edges(self, message, parent_chat_platform_id: str) -> None:
        """If a message is a forward, enqueue its source as a spider seed.

        Telethon exposes the forward source via ``message.fwd_from`` (raw API).
        We extract:
          - ``from_id``: a Peer object for the original sender (User/Channel/Chat)
          - ``channel_post``: present when forwarded from a channel post

        For each source we enqueue ONE row into ``telegram_spider_queue``:
          - source = 'forward'
          - priority = 6 (forward signals are weak compared to direct seeds)
          - title = best-effort label (we do not fetch the entity here — that
            costs an API roundtrip; the spider worker resolves later)

        Idempotent: ON CONFLICT DO NOTHING via the unique platform_chat_id index.
        """
        fwd = getattr(message, "fwd_from", None)
        if fwd is None:
            return

        # Resolve the forward source to a platform chat/user ID string.
        from_id = getattr(fwd, "from_id", None)
        source_platform_id: str | None = None
        if from_id is not None:
            # PeerChannel / PeerChat / PeerUser carry channel_id / chat_id / user_id.
            for attr in ("channel_id", "chat_id", "user_id"):
                v = getattr(from_id, attr, None)
                if v is not None:
                    source_platform_id = str(v)
                    break

        # Don't enqueue self-forwards or empty sources.
        if not source_platform_id or source_platform_id == parent_chat_platform_id:
            return

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO telegram_spider_queue
                    (platform_chat_id, source, priority, status, collected_at)
                VALUES ($1, 'forward', 6, 'pending', NOW())
                ON CONFLICT (platform_chat_id) DO NOTHING
                """,
                source_platform_id,
            )

    async def _upsert_sender(self, worker: "TelegramWorker", platform_user_id) -> str | None:
        try:
            user = await worker.client.get_entity(platform_user_id)
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO telegram_users (platform_user_id, username, first_name, last_name, phone, is_bot, is_deleted, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                    ON CONFLICT (platform_user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        phone = COALESCE(EXCLUDED.phone, telegram_users.phone),
                        is_bot = COALESCE(EXCLUDED.is_bot, telegram_users.is_bot),
                        is_deleted = EXCLUDED.is_deleted,
                        updated_at = NOW()
                    RETURNING id
                """,
                str(user.id), user.username, user.first_name, user.last_name,
                getattr(user, "phone", None),
                getattr(user, "bot", None),  # SYNC #40: Telethon User.bot
                getattr(user, "deleted", False),
                )
                self._archive_raw_payload(
                    artifact_id=f"users/{user.id}",
                    payload=_telethon_payload(user),
                    target_tables=["telegram_users"],
                    metadata={
                        "platform_user_id": str(user.id),
                        "username": getattr(user, "username", None),
                        "collection_account": getattr(worker.account, "name", None),
                        "ingest_path": self.INGEST_PATH,
                        "raw_payload_kind": "user",
                    },
                )
                return row['id']
        except Exception:
            try:
                async with self.pool.acquire() as conn:
                    row = await conn.fetchrow("""
                        INSERT INTO telegram_users (platform_user_id, is_deleted, updated_at)
                        VALUES ($1, TRUE, NOW())
                        ON CONFLICT (platform_user_id) DO UPDATE SET
                            is_deleted = TRUE, updated_at = NOW()
                        RETURNING id
                    """, str(platform_user_id))
                    return row['id'] if row else None
            except Exception:
                return None

    @staticmethod
    def _msg_refs(message):
        """Extract (reply_to, fwd_chat, fwd_msg, via_bot) as strings for the
        conversation-threading / forward-chain columns (were 0% populated)."""
        def _s(v):
            return str(v) if v not in (None, 0) else None
        reply_to = _s(getattr(message, "reply_to_msg_id", None))
        via_bot = _s(getattr(message, "via_bot_id", None))
        fwd_chat = fwd_msg = None
        fwd = getattr(message, "fwd_from", None)
        if fwd is not None:
            from_id = getattr(fwd, "from_id", None)
            if from_id is not None:
                for attr in ("channel_id", "chat_id", "user_id"):
                    v = getattr(from_id, attr, None)
                    if v is not None:
                        fwd_chat = _s(v)
                        break
            fwd_msg = _s(getattr(fwd, "channel_post", None) or getattr(fwd, "saved_from_msg_id", None))
        return reply_to, fwd_chat, fwd_msg, via_bot

    async def _upsert_message(self, message, chat_id, sender_uuid, worker: "TelegramWorker | None" = None):
        async with self.pool.acquire() as conn:
            chat_row = await conn.fetchrow("SELECT id FROM telegram_chats WHERE platform_chat_id = $1", str(chat_id))
            chat_uuid = chat_row['id'] if chat_row else None

            media_type = None
            if message.photo: media_type = 'photo'
            elif message.video: media_type = 'video'
            elif message.voice: media_type = 'voice'

            # Namespace message ID by chat to avoid global unique-constraint collisions
            # (different Telegram chats reuse low message IDs starting from 1).
            platform_msg_id = f"{chat_id}:{message.id}"
            reply_to, fwd_chat, fwd_msg, via_bot = self._msg_refs(message)
            payload = _telethon_payload(message)
            row = await conn.fetchrow("""
                INSERT INTO telegram_messages (
                    platform_message_id, chat_id, sender_id, text, caption,
                    media_type, platform_created_at, metadata,
                    reply_to_message_id, forward_from_chat_id, forward_from_message_id, via_bot_id,
                    is_pinned
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (platform_message_id) DO NOTHING
                RETURNING id
            """,
            platform_msg_id, chat_uuid, sender_uuid, message.message, getattr(message, 'caption', None),
            media_type, message.date, json.dumps(payload, default=_tg_json),
            reply_to, fwd_chat, fwd_msg, via_bot,
            bool(getattr(message, 'pinned', False) or False)
            )

            message_uuid = row["id"] if row is not None else await conn.fetchval(
                "SELECT id FROM telegram_messages WHERE platform_message_id = $1",
                platform_msg_id,
            )
            # Capture reaction counts at backfill time (item 1.11 — historical
            # messages already carry reactions in message.reactions).
            if message_uuid is not None:
                await self._capture_message_reaction_counts(conn, message_uuid, message)
                # Capture poll state if this message is a poll (item 1.12).
                await self._capture_poll(conn, message_uuid, message)
                if worker is not None:
                    await self._enumerate_poll_votes_and_enqueue(
                        worker, message, str(chat_id), message_uuid, conn=conn, chat_uuid=chat_uuid
                    )
            # Tier 6: venue/event extraction (best effort — never breaks flow).
            await self._extract_message_event(message, chat_uuid, platform_msg_id, conn=conn)
            await self._record_message_membership_signals(
                conn, message, chat_uuid, sender_uuid
            )
            await persist_discovered_links(
                conn,
                source="telegram",
                source_table="telegram_messages",
                source_record_id=platform_msg_id,
                context_id=str(chat_id),
                entity_id=str(getattr(message, "sender_id", "") or ""),
                text=" ".join(
                    v for v in (
                        getattr(message, "message", None),
                        getattr(message, "caption", None),
                    )
                    if v
                ),
                metadata={
                    "platform_message_id": platform_msg_id,
                    "platform_chat_id": str(chat_id),
                    "platform_sender_id": str(getattr(message, "sender_id", "") or ""),
                    "ingest_path": self.INGEST_PATH,
                },
            )
        if row is not None:
            self._progress_count += 1
        self._archive_raw_payload(
            artifact_id=f"messages/{platform_msg_id}",
            payload=payload,
            target_tables=["telegram_messages"],
            metadata={
                "platform_message_id": platform_msg_id,
                "platform_chat_id": str(chat_id),
                "platform_sender_id": str(getattr(message, "sender_id", "") or ""),
                "ingest_path": self.INGEST_PATH,
                "raw_payload_kind": "message",
            },
        )

    async def _capture_message_reaction_counts(self, conn, message_uuid, message) -> None:
        """Extract message.reactions.results → write telegram_reaction_counts.

        Used by both backfill (synchronous via _upsert_message) and Phase 2.2
        reactor enumeration. Safe to call when message.reactions is None.
        """
        try:
            reactions = getattr(message, "reactions", None)
            if reactions is None:
                return
            from telethon.tl.types import ReactionEmoji, ReactionCustomEmoji

            counts: dict[str, int] = {}
            total = 0
            for rc in (getattr(reactions, "results", None) or []):
                emoji_obj = getattr(rc, "reaction", None)
                count = int(getattr(rc, "count", 0) or 0)
                if count <= 0:
                    continue
                if isinstance(emoji_obj, ReactionEmoji):
                    key = emoji_obj.emoticon
                elif isinstance(emoji_obj, ReactionCustomEmoji):
                    key = f"custom:{emoji_obj.document_id}"
                else:
                    key = str(emoji_obj)
                counts[key] = count
                total += count

            if not counts:
                return

            await conn.execute(
                """
                INSERT INTO telegram_reaction_counts
                    (message_id, counts, total_reactions, refreshed_at)
                VALUES ($1, $2::jsonb, $3, NOW())
                ON CONFLICT (message_id) DO UPDATE SET
                    counts = EXCLUDED.counts,
                    total_reactions = EXCLUDED.total_reactions,
                    refreshed_at = NOW()
                """,
                message_uuid,
                _tg_jsonb(counts),
                total,
            )
        except Exception as exc:
            logger.debug("_capture_message_reaction_counts failed: %s", exc)

    async def _enumerate_reactors_and_enqueue(
        self, worker: "TelegramWorker", message, chat_platform_id: str
    ) -> int:
        """Fetch per-user reactors via GetMessageReactionsListRequest and enqueue as seeds.

        Phase 2.2: for any message with reactions, we call the Telegram API to
        get the list of users who reacted (up to `_reaction_user_cap` per emoji).
        Each reactor is written to `telegram_reactions` and enqueued as a USER
        spider seed.

        Returns the number of reactors discovered.
        """
        from telethon.tl.functions.messages import GetMessageReactionsListRequest

        reactions = getattr(message, "reactions", None)
        if reactions is None:
            return 0

        results = getattr(reactions, "results", None) or []
        if not results:
            return 0

        client = worker.client
        total_discovered = 0

        # Resolve message UUID for FK.
        platform_message_id = f"{chat_platform_id}:{message.id}"
        async with self.pool.acquire() as conn:
            msg_row = await conn.fetchrow(
                "SELECT id FROM telegram_messages WHERE platform_message_id = $1",
                platform_message_id,
            )
            if msg_row is None:
                return 0
            message_uuid = msg_row["id"]

        # Iterate each emoji type and fetch reactor list.
        for rc in results:
            emoji_obj = getattr(rc, "reaction", None)
            if emoji_obj is None:
                continue

            try:
                resp = await client(GetMessageReactionsListRequest(
                    peer=message.peer_id,
                    id=message.id,
                    reaction=emoji_obj,
                    limit=self._reaction_user_cap,
                ))
            except Exception as exc:
                logger.debug(
                    "GetMessageReactionsListRequest failed for msg=%s emoji=%s: %s",
                    message.id, emoji_obj, exc,
                )
                continue

            from telethon.tl.types import ReactionEmoji, ReactionCustomEmoji
            if isinstance(emoji_obj, ReactionEmoji):
                emoji_key = emoji_obj.emoticon
            elif isinstance(emoji_obj, ReactionCustomEmoji):
                emoji_key = f"custom:{emoji_obj.document_id}"
            else:
                emoji_key = str(emoji_obj)

            # resp.reactions is a list of MessagePeerReaction objects.
            for mpr in (getattr(resp, "reactions", None) or []):
                peer_id_obj = getattr(mpr, "peer_id", None)
                user_id = None
                if peer_id_obj is not None:
                    user_id = getattr(peer_id_obj, "user_id", None)
                if user_id is None:
                    continue

                user_platform_id = str(user_id)

                # Upsert user (best effort).
                try:
                    user_entity = await client.get_entity(user_id)
                    await self._upsert_user_full(user_entity)
                except Exception:
                    pass

                # Resolve user UUID.
                async with self.pool.acquire() as conn:
                    user_row = await conn.fetchrow(
                        "SELECT id FROM telegram_users WHERE platform_user_id = $1",
                        user_platform_id,
                    )
                    user_uuid = user_row["id"] if user_row else None

                    # Insert into telegram_reactions.
                    if user_uuid is not None:
                        is_big = bool(getattr(mpr, "big", False))
                        added_at = getattr(mpr, "date", None)
                        await conn.execute(
                            """
                            INSERT INTO telegram_reactions
                                (message_id, user_id, emoji, is_big, added_at, refreshed_at)
                            VALUES ($1, $2, $3, $4, $5, NOW())
                            ON CONFLICT (message_id, user_id, emoji) DO UPDATE SET
                                is_big = EXCLUDED.is_big,
                                added_at = COALESCE(EXCLUDED.added_at, telegram_reactions.added_at),
                                refreshed_at = NOW()
                            """,
                            message_uuid,
                            user_uuid,
                            emoji_key,
                            is_big,
                            added_at,
                        )

                    # Enqueue user as spider seed.
                    await conn.execute(
                        """
                        INSERT INTO telegram_spider_queue
                            (platform_chat_id, title, source, priority, status, collected_at)
                        VALUES ($1, $2, 'reactor', 7, 'pending', NOW())
                        ON CONFLICT (platform_chat_id) DO NOTHING
                        """,
                        user_platform_id,
                        None,  # title — we don't know it yet
                    )

                total_discovered += 1

        return total_discovered

    async def _enumerate_poll_votes_and_enqueue(
        self,
        worker: "TelegramWorker",
        message,
        chat_platform_id: str,
        message_uuid,
        *,
        conn=None,
        chat_uuid=None,
    ) -> int:
        """Fetch public/non-anonymous poll voters and store them as evidence."""
        poll_media = getattr(message, "poll", None)
        if poll_media is None or message_uuid is None:
            return 0
        poll = getattr(poll_media, "poll", None)
        if poll is None:
            return 0
        # Anonymous polls do not expose voter identities through Telegram.
        if not bool(getattr(poll, "public_voters", False)):
            return 0

        from telethon.tl.functions.messages import GetPollVotesRequest
        from telethon.tl.types import (
            MessagePeerVote,
            MessagePeerVoteInputOption,
            MessagePeerVoteMultiple,
        )

        option_hex_to_index: dict[str, int] = {}
        for idx, ans in enumerate(getattr(poll, "answers", None) or []):
            data = getattr(ans, "option", None)
            if isinstance(data, (bytes, bytearray)):
                option_hex_to_index[data.hex()] = idx

        users_seen = 0
        offset = ""
        client = worker.client
        owns_conn = conn is None
        if owns_conn:
            conn = await self.pool.acquire()
        try:
            while users_seen < self._poll_vote_user_cap:
                limit = min(100, self._poll_vote_user_cap - users_seen)
                try:
                    resp = await client(GetPollVotesRequest(
                        peer=message.peer_id,
                        id=message.id,
                        limit=limit,
                        offset=offset or None,
                    ))
                except Exception as exc:
                    logger.debug(
                        "GetPollVotesRequest failed for msg=%s: %s",
                        getattr(message, "id", "?"), exc,
                    )
                    break

                votes = getattr(resp, "votes", None) or []
                if not votes:
                    break
                user_entities = {
                    str(getattr(user, "id", "")): user
                    for user in (getattr(resp, "users", None) or [])
                    if getattr(user, "id", None) is not None
                }

                for vote in votes:
                    peer = getattr(vote, "peer", None)
                    user_id = getattr(peer, "user_id", None)
                    if user_id is None:
                        continue
                    user_platform_id = str(user_id)

                    user_entity = user_entities.get(user_platform_id)
                    if user_entity is not None:
                        await self._upsert_user_full(user_entity)
                    user_uuid = await self._ensure_telegram_user_stub(conn, user_platform_id)
                    if user_uuid is None:
                        continue

                    option_hexes: list[str] = []
                    if isinstance(vote, MessagePeerVoteMultiple):
                        for option in getattr(vote, "options", None) or []:
                            if isinstance(option, (bytes, bytearray)):
                                option_hexes.append(option.hex())
                    elif isinstance(vote, MessagePeerVote):
                        option = getattr(vote, "option", None)
                        if isinstance(option, (bytes, bytearray)):
                            option_hexes.append(option.hex())
                    elif isinstance(vote, MessagePeerVoteInputOption):
                        option = getattr(vote, "option", None)
                        if isinstance(option, (bytes, bytearray)):
                            option_hexes.append(option.hex())

                    option_indices = [
                        option_hex_to_index[hex_value]
                        for hex_value in option_hexes
                        if hex_value in option_hex_to_index
                    ]
                    voted_at = getattr(vote, "date", None)
                    await conn.execute(
                        """
                        INSERT INTO telegram_poll_votes
                            (message_id, user_id, option_indices, option_data, voted_at, refreshed_at)
                        VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, NOW())
                        ON CONFLICT (message_id, user_id) DO UPDATE SET
                            option_indices = EXCLUDED.option_indices,
                            option_data = EXCLUDED.option_data,
                            voted_at = COALESCE(EXCLUDED.voted_at, telegram_poll_votes.voted_at),
                            refreshed_at = NOW()
                        """,
                        message_uuid,
                        user_uuid,
                        _tg_jsonb(option_indices),
                        _tg_jsonb(option_hexes),
                        voted_at,
                    )
                    await conn.execute(
                        """
                        INSERT INTO telegram_spider_queue
                            (platform_chat_id, title, source, priority, status, collected_at)
                        VALUES ($1, $2, 'poll_voter', 7, 'pending', NOW())
                        ON CONFLICT (platform_chat_id) DO NOTHING
                        """,
                        user_platform_id,
                        None,
                    )

                    if chat_uuid is None:
                        chat_row = await conn.fetchrow(
                            "SELECT id FROM telegram_chats WHERE platform_chat_id = $1",
                            str(chat_platform_id),
                        )
                        resolved_chat_uuid = chat_row["id"] if chat_row else None
                    else:
                        resolved_chat_uuid = chat_uuid
                    if resolved_chat_uuid is not None:
                        await self._upsert_chat_member_observation(
                            conn,
                            resolved_chat_uuid,
                            user_uuid,
                            role="member",
                            last_seen_at=voted_at,
                        )
                    users_seen += 1
                    if users_seen >= self._poll_vote_user_cap:
                        break

                next_offset = getattr(resp, "next_offset", None)
                if not next_offset:
                    break
                offset = next_offset
        finally:
            if owns_conn:
                await self.pool.release(conn)
        return users_seen

    async def _capture_poll(self, conn, message_uuid, message) -> None:
        """Extract a Telegram poll into telegram_polls.

        Telethon shape: ``message.poll`` is a ``MessageMediaPoll`` carrying
        ``.poll`` (the question + options) and ``.results`` (vote counts).
        Quiz polls carry ``correct_answers`` in results.

        Telegram delivers poll *results* asynchronously after voting; full
        tallies are fetched on demand via ``GetPollResultsRequest``. We do the
        cheap read-from-message-payload here; the periodic monitor cron can
        re-call this with a fetched fresh poll.
        """
        try:
            poll_media = getattr(message, "poll", None)
            if poll_media is None:
                return
            poll = getattr(poll_media, "poll", None)
            results = getattr(poll_media, "results", None)
            if poll is None:
                return

            poll_id = str(getattr(poll, "id", ""))
            question_obj = getattr(poll, "question", None)
            # Telethon may return question as a TextWithEntities-like object;
            # extract .text or fall back to str().
            question_text = (
                getattr(question_obj, "text", None)
                if question_obj is not None else None
            ) or (str(question_obj) if question_obj is not None else None)

            options = []
            for ans in (getattr(poll, "answers", None) or []):
                a_text_obj = getattr(ans, "text", None)
                a_text = (
                    getattr(a_text_obj, "text", None)
                    if a_text_obj is not None else None
                ) or (str(a_text_obj) if a_text_obj is not None else None)
                a_data = getattr(ans, "option", None)
                # bytes → hex str so JSON encoder doesn't choke
                if isinstance(a_data, (bytes, bytearray)):
                    a_data = a_data.hex()
                options.append({"text": a_text, "data": a_data})

            vote_counts = []
            total_voters = 0
            quiz_correct_idx = None
            if results is not None:
                total_voters = int(getattr(results, "total_voters", 0) or 0)
                vote_results = getattr(results, "results", None) or []
                for idx, rr in enumerate(vote_results):
                    voters = int(getattr(rr, "voters", 0) or 0)
                    correct = bool(getattr(rr, "correct", False))
                    vote_counts.append({"option": idx, "voters": voters, "correct": correct})
                    if correct:
                        quiz_correct_idx = idx

            await conn.execute(
                """
                INSERT INTO telegram_polls (
                    message_id, poll_id, question, options,
                    total_voters, vote_counts,
                    is_closed, is_anonymous, allows_multiple, quiz_correct_idx, refreshed_at
                ) VALUES ($1, $2, $3, $4::jsonb, $5, $6::jsonb, $7, $8, $9, $10, NOW())
                ON CONFLICT (message_id) DO UPDATE SET
                    poll_id = EXCLUDED.poll_id,
                    question = EXCLUDED.question,
                    options = EXCLUDED.options,
                    total_voters = EXCLUDED.total_voters,
                    vote_counts = EXCLUDED.vote_counts,
                    is_closed = EXCLUDED.is_closed,
                    is_anonymous = EXCLUDED.is_anonymous,
                    allows_multiple = EXCLUDED.allows_multiple,
                    quiz_correct_idx = EXCLUDED.quiz_correct_idx,
                    refreshed_at = NOW()
                """,
                message_uuid,
                poll_id,
                question_text,
                _tg_jsonb(options),
                total_voters,
                _tg_jsonb(vote_counts),
                bool(getattr(poll, "closed", False)),
                bool(getattr(poll, "public_voters", False)) is False,  # is_anonymous = NOT public_voters
                bool(getattr(poll, "multiple_choice", False)),
                quiz_correct_idx,
            )
        except Exception as exc:
            logger.debug("_capture_poll failed: %s", exc)

    async def _collect_profile_photo(self, worker: "TelegramWorker", entity, chat_id: str, chat_name: str):
        cid = f"profile_{chat_id}"
        if self.is_known(cid):
            return
        try:
            photo = await worker.client.download_profile_photo(entity, bytes)
            if photo:
                await self.download_media({
                    "entity_id": chat_id,
                    "entity_name": chat_name,
                    "content_type": "profile_photo",
                    "content_id": cid,
                    "data": photo,
                    "extension": "jpg",
                    # source_url for the profile page — only well-defined for
                    # public entities (with a username). Private chats have no
                    # web-openable profile page, so leave None there.
                    "chat_username": getattr(entity, "username", None),
                }, worker=worker)
        except Exception as e:
            logger.debug("Profile photo download failed for %s: %s", chat_name, e)

    @staticmethod
    def _build_telegram_source_url(item: dict) -> str | None:
        """Deep-link URL that opens this media's originating message OR the
        entity's profile page. Populates media_items.source_url so downstream
        consumers can trace a stored file back to the exact source message.

        Callers with a fully-formed URL (e.g. stories, which use a different
        path than regular messages) can bypass this helper by setting
        ``item['source_url_override']``.

        Format rules for the default derivation:
          - profile_photo, chat_username set     -> https://t.me/<username>
          - profile_photo, positive numeric user -> tg://user?id=<uid>
                                                     (stable URI, not
                                                     web-openable)
          - profile_photo, otherwise             -> None
          - regular media, chat_username set     -> https://t.me/<username>/<msg_id>
          - regular media, supergroup / group    -> https://t.me/c/<chat_id>/<msg_id>
                                                     (member-openable)
          - regular media, positive user chat    -> tg://openmessage?user_id=X
                                                                      &message_id=Y
                                                     (stable URI mirror of
                                                     the whatsapp:// scheme
                                                     used for WA media —
                                                     preserves lineage
                                                     downstream even though
                                                     not web-openable)

        Callers must set ``item['chat_username']`` (may be None) and, for
        non-profile media, ``item['message_id']``. Both are cheap to add at
        the call sites where the message + chat entity are already in scope.
        """
        override = item.get("source_url_override")
        if override:
            return override
        chat_username = item.get("chat_username")
        chat_id = item.get("entity_id")
        # profile_photo and user_profile_photo both map to the entity page,
        # not a specific message. They share the /<username> shape.
        if item.get("content_type") in ("profile_photo", "user_profile_photo"):
            if chat_username:
                return f"https://t.me/{chat_username}"
            # No public URL for a private user's profile page. Fall back to
            # tg://user?id=<uid> — clients understand it, and it's a stable
            # identifier for downstream unifiedanalyzer joins even though
            # it isn't a web-openable link. Only emit when we have a
            # positive numeric user id (chat_ids that could be groups
            # aren't valid tg://user targets).
            try:
                raw = str(chat_id) if chat_id is not None else ""
            except Exception:
                raw = ""
            if raw.isdigit() and int(raw) > 0:
                return f"tg://user?id={raw}"
            return None
        message_id = item.get("message_id")
        if message_id is None:
            return None
        if chat_username:
            return f"https://t.me/{chat_username}/{message_id}"
        # Private/no-username fallback. Supergroup chat_ids look like
        # "-1001234567890"; the /c/ deep-link format wants just the numeric
        # portion after the -100. Regular groups have -<pos_int> and use the
        # positive value. Users (positive chat_ids) have no /c/ URL form —
        # we emit tg://openmessage?user_id=<uid>&message_id=<mid> instead,
        # mirroring the whatsapp:// URI scheme convention: not publicly
        # openable but preserves message lineage as a stable identifier.
        try:
            raw = str(chat_id)
        except Exception:
            return None
        if not raw or raw.startswith("+"):
            return None
        if raw.startswith("-100"):
            raw = raw[4:]
        elif raw.startswith("-"):
            raw = raw[1:]
        else:
            # Positive chat_id = private user DM. tg:// URI keeps the
            # (user_id, message_id) tuple identifiable downstream.
            if raw.isdigit():
                return f"tg://openmessage?user_id={raw}&message_id={message_id}"
            return None
        if not raw.isdigit():
            return None
        return f"https://t.me/c/{raw}/{message_id}"

    async def download_media(self, item: dict, worker: "TelegramWorker | None" = None):
        cid = item["content_id"]
        if self.is_known(cid):
            return

        filename = self.build_filename(
            item["entity_id"], item["entity_name"],
            item["content_type"], cid, extension=item.get("extension", "jpg")
        )

        try:
            if "data" in item:
                data = item["data"]
            else:
                client = worker.client if worker else self._primary_client
                data = await client.download_media(item["media"], bytes)

            if not data:
                return

            source_url = self._build_telegram_source_url(item)
            metadata = {
                "entity_id": item["entity_id"],
                "entity_name": item["entity_name"],
                "content_type": item["content_type"],
                "content_id": cid,
                "filename": filename,
                "source_url": source_url,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {}),
                "rebuild_target_tables": ["media_items", "telegram_messages"],
            }
            artifact = write_atomic_artifact(
                source=self.SOURCE_NAME,
                artifact_id=f"{item['entity_id']}:{cid}",
                artifact_kind="media_blob",
                data=data,
                extension=item.get("extension", "jpg"),
                metadata=metadata,
                root=VAULT_ROOT,
            )
            if not artifact.path:
                raise RuntimeError(f"vault artifact write failed: {artifact.error}")
            metadata["vault_artifact"] = {
                "ok": artifact.ok,
                "partial": artifact.partial,
                "path": artifact.relative_path,
                "blob_path": artifact.blob_relative_path,
                "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
                "duplicate_blob": artifact.duplicate_blob,
                "error": artifact.error,
            }

            await self.insert_media_item(
                entity_id=item["entity_id"], entity_name=item["entity_name"],
                content_type=item["content_type"], content_id=cid,
                filename=filename, file_path=str(artifact.path),
                file_size=artifact.file_size, sha256=artifact.sha256, metadata=metadata,
                source_url=source_url,
                kind=item.get("kind"),
            )
            if artifact.partial:
                await self.send_to_dlq(
                    item["entity_id"],
                    cid,
                    f"vault artifact partial: {artifact.error}",
                )
            self._known_ids.add(cid)
        except Exception as e:
            if _is_flood_wait(e):
                raise
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))

    async def _handle_photo(self, worker: "TelegramWorker", message, chat_id: str,
                            chat_name: str, chat_username: str | None = None):
        await self.download_media({
            "entity_id": chat_id,
            "entity_name": chat_name,
            "content_type": "photo",
            "content_id": str(message.id),
            "media": message.media.photo,
            "raw": message.to_dict(),
            # Deep-link URL population (media_items.source_url) — see
            # _build_telegram_source_url. chat_username is None for private
            # chats; that falls back to https://t.me/c/<numeric>/<msg_id>.
            "chat_username": chat_username,
            "message_id": message.id,
        }, worker=worker)

    async def _handle_document(self, worker: "TelegramWorker", message, chat_id: str,
                               chat_name: str, mime: str,
                               chat_username: str | None = None) -> bool:
        """Classify + download a document attachment per Tier 3 spec.

        Returns True if the document was downloaded, False if it was skipped
        (executable/code/unknown type, or an animated sticker).
        """
        from telethon.tl.types import (
            DocumentAttributeFilename,
            DocumentAttributeSticker,
            DocumentAttributeAnimated,
            DocumentAttributeAudio,
            DocumentAttributeVideo,
        )
        from src.core.document_filter import classify_document

        doc = message.media.document
        attrs = getattr(doc, "attributes", []) or []
        filename = None
        is_sticker = is_animated = is_audio = is_video = False
        for a in attrs:
            if isinstance(a, DocumentAttributeFilename):
                filename = a.file_name
            elif isinstance(a, DocumentAttributeSticker):
                is_sticker = True
            elif isinstance(a, DocumentAttributeAnimated):
                is_animated = True
            elif isinstance(a, DocumentAttributeAudio):
                is_audio = True
            elif isinstance(a, DocumentAttributeVideo):
                is_video = True

        decision = classify_document(
            mime, filename,
            is_sticker=is_sticker, is_animated=is_animated,
            is_audio=is_audio, is_video=is_video,
        )
        if not decision.download:
            logger.debug("Telegram doc skipped (%s): chat=%s msg=%s",
                         decision.reason, chat_name, message.id)
            return False

        # Extension: prefer the real filename, else derive from MIME.
        if filename and "." in filename:
            ext = filename.rsplit(".", 1)[-1].lower()[:12]
        else:
            ext = _ext_from_mime(mime) or (mime.split("/")[-1] if "/" in mime else "bin")

        await self.download_media({
            "entity_id": chat_id,
            "entity_name": chat_name,
            "content_type": decision.content_type,
            "content_id": str(message.id),
            "media": message.media.document,
            "extension": ext,
            "raw": message.to_dict(),
            # Deep-link population — see _build_telegram_source_url.
            "chat_username": chat_username,
            "message_id": message.id,
        }, worker=worker)
        return True

    @staticmethod
    def _entity_from_peer(peer, umap: dict, cmap: dict):
        """Resolve a Telethon Peer to its entity using the users/chats maps that
        come back inside a stories response (avoids an extra get_entity call)."""
        if peer is None:
            return None
        uid = getattr(peer, "user_id", None)
        if uid is not None:
            return umap.get(uid)
        cid = getattr(peer, "channel_id", None)
        if cid is not None:
            return cmap.get(cid)
        chid = getattr(peer, "chat_id", None)
        if chid is not None:
            return cmap.get(chid)
        return None

    async def _download_story_items(self, worker, ent_id, ent_name, ent_username, story_items):
        """Download a peer's active story media into media_items (kind='story')."""
        for story in story_items or []:
            if self._stop.is_set():
                break
            story_id = getattr(story, "id", None)
            if not story_id:
                continue
            cid = f"story_{ent_id}_{story_id}"
            if self.is_known(cid):
                continue
            media = getattr(story, "media", None)
            if not media:
                continue
            is_video = hasattr(media, "video")
            story_url = f"https://t.me/{ent_username}/s/{story_id}" if ent_username else None
            try:
                await self.download_media({
                    "entity_id": str(ent_id),
                    "entity_name": ent_name,
                    "content_type": "story_video" if is_video else "story",
                    # Normalize ephemeral onto media_items.kind='story' (same
                    # convention as Instagram) so the dashboard surfaces it.
                    "kind": "story",
                    "content_id": cid,
                    "media": media,
                    "extension": "mp4" if is_video else "jpg",
                    "raw": story.to_dict(),
                    "source_url_override": story_url,
                }, worker=worker)
            except Exception as e:
                logger.debug("story download failed %s: %s", cid, e)

    async def _known_user_ids_for_stories(self, limit: int) -> list[str]:
        """A bounded, cheaply-sampled batch of discovered user ids to probe for
        PUBLIC stories (users we've come across but may not follow). TABLESAMPLE
        keeps it cheap on a large table; best-effort."""
        if not self.pool or limit <= 0:
            return []
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT platform_user_id FROM telegram_users "
                    "TABLESAMPLE SYSTEM (2) "
                    "WHERE platform_user_id ~ '^[0-9]+$' LIMIT $1",
                    limit,
                )
            return [r["platform_user_id"] for r in rows]
        except Exception:
            return []

    async def _backfill_message_locations(self, batch: int = 500) -> int:
        """Tier 5: extract shared/live-location coordinates out of message
        metadata into telegram_message_locations (structured + queryable).

        Reads EXISTING metadata only (the raw geo was already stored) — never
        touches the hot message INSERT path. Bounded batch per cycle; the partial
        idx_tg_messages_geo makes "find geo messages not yet extracted" cheap.
        Covers MessageMediaGeo / GeoLive / Venue.
        """
        if not self.pool:
            return 0
        try:
            async with self.pool.acquire() as conn:
                res = await conn.execute(
                    """
                    INSERT INTO telegram_message_locations
                        (platform_message_id, chat_id, latitude, longitude,
                         is_live, venue_title, venue_address)
                    SELECT m.platform_message_id, m.chat_id,
                           (m.metadata->'media'->'geo'->>'lat')::double precision,
                           (m.metadata->'media'->'geo'->>'long')::double precision,
                           (m.metadata->'media'->>'_') = 'MessageMediaGeoLive',
                           m.metadata->'media'->>'title',
                           m.metadata->'media'->>'address'
                    FROM telegram_messages m
                    WHERE (m.metadata->'media'->'geo'->>'lat') IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM telegram_message_locations l
                          WHERE l.platform_message_id = m.platform_message_id)
                    LIMIT $1
                    ON CONFLICT (platform_message_id) DO NOTHING
                    """,
                    batch,
                )
            try:
                n = int(res.split()[-1])
            except (ValueError, IndexError):
                n = 0
            if n:
                logger.info("telegram: backfilled %d shared-location message(s)", n)
            return n
        except Exception as e:
            logger.debug("_backfill_message_locations failed: %s", e)
            return 0

    async def _scan_stories(self, worker: "TelegramWorker", targets: list[str]):
        try:
            from telethon.tl.functions.stories import GetPeerStoriesRequest
        except ImportError:
            return
        # GetAllStoriesRequest (the whole "tray") is newer — import defensively so
        # an older Telethon still runs the per-target path below.
        try:
            from telethon.tl.functions.stories import GetAllStoriesRequest
        except ImportError:
            GetAllStoriesRequest = None

        # 1) PRIMARY: the whole story tray for EACH account = every followed
        #    CONTACT's active stories. Telegram stories come from users you
        #    follow (the collector's `targets` are channels), so this is where
        #    the volume is. One call per account covers all its contacts.
        for w in (self._workers if GetAllStoriesRequest is not None else []):
            if self._stop.is_set():
                break
            if getattr(w, "state", None) not in (None, SessionState.CONNECTED):
                continue
            try:
                res = await w.client(GetAllStoriesRequest())
            except Exception as e:
                logger.debug("GetAllStories failed on w=%s: %s",
                             getattr(w, "worker_id", "?"), e)
                continue
            umap = {u.id: u for u in (getattr(res, "users", None) or [])}
            cmap = {c.id: c for c in (getattr(res, "chats", None) or [])}
            for ps in (getattr(res, "peer_stories", None) or []):
                ent = self._entity_from_peer(getattr(ps, "peer", None), umap, cmap)
                if ent is None:
                    continue
                ent_id = getattr(ent, "id", None)
                ent_name = (getattr(ent, "title", None) or getattr(ent, "username", None)
                            or getattr(ent, "first_name", None) or str(ent_id))
                ent_username = getattr(ent, "username", None)
                await self._download_story_items(
                    w, ent_id, ent_name, ent_username, getattr(ps, "stories", None) or [])

        # 2) SECONDARY: explicit configured targets + a sampled batch of
        #    discovered users — catches PUBLIC stories from users we've come
        #    across but don't follow. Cross-account resolved (owning account).
        extra = list(targets or [])
        extra += await self._known_user_ids_for_stories(
            int(os.getenv("TELEGRAM_STORY_USER_BATCH", "60")))
        seen: set[str] = set()
        for target in extra:
            if self._stop.is_set():
                break
            t = str(target)
            if not t or t in seen:
                continue
            seen.add(t)
            try:
                owner_w, entity = await self._resolve_entity_any_worker(worker, t)
            except EntityUnresolvable:
                continue
            except Exception:
                continue
            ent_id = getattr(entity, "id", None)
            ent_name = (getattr(entity, "title", None) or getattr(entity, "username", None)
                        or getattr(entity, "first_name", None) or str(ent_id))
            ent_username = getattr(entity, "username", None)
            try:
                result = await owner_w.client(GetPeerStoriesRequest(peer=entity))
                stories = getattr(result, "stories", None)
                if not stories:
                    continue
                await self._download_story_items(
                    owner_w, ent_id, ent_name, ent_username,
                    getattr(stories, "stories", None) or [])
            except Exception as e:
                logger.debug("Story fetch failed for %s: %s", ent_name, e)

    async def _poll_admin_logs(self, entity):
        # Placeholder — telegramcollector/services/collector/admin_log_poller.py
        # is not yet ported. Tracked in deferred plan.
        pass

    async def _process_join_queue(self):
        # Placeholder — telegramcollector/services/collector/group_manager.py
        # join queue is not yet ported. Tracked in deferred plan.
        pass

    # ==================================================================
    # Realtime ingestion — ported from
    # telegramcollector/services/collector/realtime_worker.py
    # ==================================================================

    async def _register_realtime_handlers(self):
        """Attach @client.on(NewMessage/Edited/Deleted/ChatAction/UserUpdate/Reactions)
        handlers to every connected worker. Idempotent (guarded) and cheap — telethon's
        per-client update loop fires these as soon as they're registered, independent of
        whether collect() is parked or busy backfilling. We therefore call this EARLY in
        collect() so LIVE messages stream in during the initial historical backfill
        (previously realtime only started AFTER backfill finished, so live capture stalled
        for hours behind a multi-thousand-chat backfill)."""
        from telethon import events

        if not self._workers:
            self._workers = await self._spawn_workers()
        if not self._workers:
            logger.error("_register_realtime_handlers: no Telegram workers connected")
            return
        registered = 0
        for worker in self._workers:
            if self._register_realtime_handlers_for_worker(worker, events):
                registered += 1
        if registered == 0:
            logger.debug("telegram realtime handlers already registered for all connected workers")
        self._handlers_registered = True

    def _register_realtime_handlers_for_worker(self, worker, events_module=None) -> bool:
        """Attach realtime handlers to one connected worker client, once per client."""
        if events_module is None:
            from telethon import events as events_module

        client = getattr(worker, "client", None)
        if client is None:
            logger.warning(
                "[worker=%d account=%s] realtime handler registration skipped: no client",
                worker.worker_id, worker.account.name,
            )
            return False

        client_key = (worker.worker_id, id(client))
        if client_key in self._realtime_handler_clients:
            return False

        client.add_event_handler(
            lambda e, w=worker: self._on_new_message(w, e),
            events_module.NewMessage(),
        )
        client.add_event_handler(
            lambda e, w=worker: self._on_message_edited(w, e),
            events_module.MessageEdited(),
        )
        client.add_event_handler(
            lambda e, w=worker: self._on_message_deleted(w, e),
            events_module.MessageDeleted(),
        )
        client.add_event_handler(
            lambda e, w=worker: self._on_chat_action(w, e),
            events_module.ChatAction(),
        )
        client.add_event_handler(
            lambda e, w=worker: self._on_user_update(w, e),
            events_module.UserUpdate(),
        )
        # Reactions — Telethon delivers these via Raw updates rather than a
        # dedicated event class. We listen for both message-level reaction
        # updates (humans on channels/groups) and bot-message reactions.
        try:
            from telethon.tl.types import (
                UpdateMessageReactions,
                UpdateBotMessageReactions,
            )
            client.add_event_handler(
                lambda e, w=worker: self._on_raw_reactions(w, e),
                events_module.Raw(types=[UpdateMessageReactions, UpdateBotMessageReactions]),
            )
        except Exception as exc:
            # Older Telethon may not expose UpdateBotMessageReactions; degrade.
            logger.warning(
                "Reaction event registration failed (older Telethon?): %s",
                exc,
            )

        self._realtime_handler_clients.add(client_key)
        logger.info(
            "[worker=%d account=%s] realtime handlers registered",
            worker.worker_id, worker.account.name,
        )
        return True

    async def collect_realtime(self):
        """Register Telethon event handlers on every connected worker and run forever.

        This is the @client.on(events.NewMessage) listener equivalent. New /
        edited / deleted messages and chat-action / user-update events are
        persisted to the unified telegram_* schema. Media is downloaded
        inline via download_message_media() rather than enqueued to Redis
        (the unified collector replaces the microservices' Redis queue).

        Runs until self._stop is set.
        """
        # Handlers may already be registered (we register them EARLY in collect()
        # so live messages stream during the initial backfill instead of waiting
        # for it to finish). Idempotent.
        await self._register_realtime_handlers()
        if not self._workers:
            logger.error("collect_realtime: no Telegram workers connected — bailing")
            return
        self._realtime_running = True
        logger.info(
            "Realtime listener running across %d worker(s); awaiting events…",
            len(self._workers),
        )
        # Independent resolve-only sweep so dead chats get reclassified even while
        # the drain gather below is busy deep-backfilling a large channel for hours.
        if not getattr(self, "_sweep_task", None):
            self._sweep_task = asyncio.create_task(self._resolve_sweep_loop())
        # Park until stop. Telethon delivers events under each client's own task.
        # While parked we ALSO keep historical backfill flowing: every
        # TELEGRAM_BACKFILL_DRAIN_INTERVAL seconds drain the spider/backfill queue
        # across ALL connected workers in parallel (SKIP LOCKED makes this safe).
        # This is the "all accounts backfill + scrape at once" behaviour — live
        # listening and historical catch-up run concurrently instead of backfill
        # only happening once per (re)launch.
        drain_interval = float(os.getenv("TELEGRAM_BACKFILL_DRAIN_INTERVAL", "60"))
        health_interval = float(os.getenv("TELEGRAM_HEALTH_INTERVAL", "60"))
        spider_on = os.getenv("TELEGRAM_SPIDER_ENABLED", "true").lower() == "true"
        last_drain = asyncio.get_event_loop().time()
        last_health = asyncio.get_event_loop().time()
        while self._realtime_running and not self._stop.is_set():
            await asyncio.sleep(1.0)
            now = asyncio.get_event_loop().time()
            # SELF-HEAL: reconnect any worker whose Telethon (MTProto) client dropped.
            # The container healthcheck only tests HTTP, and the worker watchdog exempts
            # realtime sources from restart — so a dead connection used to sit silently
            # (this happened: telegram dead ~26h "Cannot send requests while disconnected").
            # Reconnecting the same client preserves the registered event handlers.
            if now - last_health >= health_interval:
                last_health = now
                for w in self._workers:
                    try:
                        if not w.client.is_connected():
                            logger.warning("telegram: worker=%d account=%s DISCONNECTED — reconnecting",
                                           w.worker_id, w.account.name)
                            await w.client.connect()
                            logger.info("telegram: worker=%d reconnected", w.worker_id)
                    except Exception as exc:
                        logger.error("telegram: worker=%d reconnect failed: %s", w.worker_id, exc)
            if not spider_on:
                continue
            if now - last_drain < drain_interval:
                continue
            last_drain = now
            try:
                spider_workers = [
                    w for w in self._workers
                    if self._is_spider_allowed(w)
                ]
                if not spider_workers:
                    logger.debug("realtime backfill drain skipped: no worker matches TELEGRAM_SPIDER_ACCOUNTS")
                    continue
                await asyncio.gather(
                    *(self._process_spider_queue(w) for w in spider_workers),
                    return_exceptions=True,
                )
            except Exception as exc:
                logger.debug("realtime backfill drain failed: %s", exc)
            # Bounded sweep: download profile photos for users that lack one
            # (user: "why doesn't tg collector scrape photo of users").
            try:
                await self._collect_user_photos_pass(
                    self._workers[0], batch=int(os.getenv("TELEGRAM_USER_PHOTO_BATCH", "15"))
                )
            except Exception as exc:
                logger.debug("user photo pass failed: %s", exc)

    async def _write_realtime_message_with_retry(
        self,
        worker: "TelegramWorker",
        message,
        chat_id: int,
        is_edit: bool = False,
    ):
        attempts = max(1, int(getattr(self, "_realtime_write_attempts", 3) or 3))
        delay = max(0.0, float(getattr(self, "_realtime_write_retry_delay", 0.75) or 0.0))
        for attempt in range(1, attempts + 1):
            try:
                await self._write_realtime_message(worker, message, chat_id, is_edit=is_edit)
                if attempt > 1:
                    logger.info(
                        "telegram realtime write recovered after %d attempt(s): chat=%s msg=%s edit=%s",
                        attempt, chat_id, getattr(message, "id", None), is_edit,
                    )
                return
            except Exception as exc:
                if attempt >= attempts or not _is_transient_realtime_write_error(exc):
                    raise
                logger.warning(
                    "telegram realtime write transient failure; retrying %d/%d: chat=%s msg=%s edit=%s error=%s",
                    attempt + 1, attempts, chat_id, getattr(message, "id", None), is_edit, exc,
                )
                if delay:
                    await asyncio.sleep(delay * attempt)

    async def _on_new_message(self, worker: "TelegramWorker", event):
        try:
            chat_id = event.chat_id
            if self._hub_group_id is not None and chat_id == self._hub_group_id:
                return  # discard hub-group messages
            message = event.message
            await self._write_realtime_message_with_retry(worker, message, chat_id)
            # Sender resolution hits the network and can raise ChannelPrivateError
            # for private/restricted channels (or if we were removed). Isolate it so
            # it neither aborts persistence nor skips the media download below, and
            # downgrade the noise to debug.
            try:
                sender = await event.get_sender()
                if sender is not None:
                    await self._upsert_user_full(sender)
            except Exception as exc:
                logger.debug("get_sender failed (private/restricted channel?): %s", exc)
            if getattr(message, "media", None) is not None:
                # Download inline rather than queueing.
                try:
                    await self.download_message_media(message, worker=worker, chat_id=chat_id)
                except Exception as exc:
                    logger.debug("realtime media download failed: %s", exc)
        except Exception as exc:
            logger.error("_on_new_message error: %s", exc, exc_info=True)

    async def _on_message_edited(self, worker: "TelegramWorker", event):
        try:
            chat_id = event.chat_id
            message = event.message
            await self._write_realtime_message_with_retry(worker, message, chat_id, is_edit=True)
        except Exception as exc:
            logger.error("_on_message_edited error: %s", exc, exc_info=True)

    async def _on_message_deleted(self, worker: "TelegramWorker", event):
        try:
            from telethon.utils import resolve_id
            # event.chat_id is the marked id (-100… for channels); the stored
            # platform_message_id uses the bare id, so normalize first. Telegram can
            # deliver delete events without chat context. Message IDs are only unique
            # inside a chat, so a chatless delete cannot be matched safely.
            raw_chat = event.chat_id
            chat_id = None
            if raw_chat is not None:
                try:
                    chat_id, _ = resolve_id(raw_chat)
                except Exception:
                    chat_id = raw_chat
            if chat_id is None:
                logger.debug(
                    "telegram delete event without chat context skipped: message_ids=%s",
                    event.deleted_ids or [],
                )
                return
            # capture WHEN we observed the deletion (telegram doesn't tell us the
            # exact delete time; observation time is the best available).
            deleted_at_iso = datetime.now(tz=timezone.utc).isoformat()
            patch = json.dumps({"deleted": True, "deleted_at": deleted_at_iso})
            for msg_id in (event.deleted_ids or []):
                async with self.pool.acquire() as conn:
                    # Merge a {deleted, deleted_at} patch into metadata; no-op if the
                    # row doesn't exist (deletion of a message we never saw).
                    await conn.execute("""
                        UPDATE telegram_messages
                        SET metadata = COALESCE(metadata,'{}'::jsonb) || $2::jsonb
                        WHERE platform_message_id = $1
                    """, f"{chat_id}:{msg_id}", patch)
        except Exception as exc:
            logger.error("_on_message_deleted error: %s", exc, exc_info=True)

    async def _on_chat_action(self, worker: "TelegramWorker", event):
        """Translate Telethon chat actions into telegram_chat_members upserts."""
        try:
            from telethon.utils import resolve_id
            chat_id, _ = resolve_id(event.chat_id)  # marked (-100…) -> bare id
            role = "member"
            if getattr(event, "user_kicked", False):
                role = "banned"
            elif getattr(event, "user_left", False):
                role = "left"
            user_ids: list[int] = []
            user_ids.extend(self._coerce_telegram_user_ids(getattr(event, "user_id", None)))
            user_ids.extend(self._coerce_telegram_user_ids(getattr(event, "user_ids", None)))
            user_ids.extend(self._coerce_telegram_user_ids(getattr(event, "user", None)))
            user_ids.extend(self._coerce_telegram_user_ids(getattr(event, "users", None)))
            user_ids = list(dict.fromkeys(user_ids))
            if not user_ids:
                return
            async with self.pool.acquire() as conn:
                # telegram_chat_members.chat_id/user_id are uuids (FK) — resolve
                # the bare platform ids to internal uuids; skip if not collected yet.
                chat_uuid = await conn.fetchval(
                    "SELECT id FROM telegram_chats WHERE platform_chat_id = $1",
                    str(chat_id))
                if chat_uuid is None:
                    return
                observed_at = datetime.now(timezone.utc)
                for user_id in user_ids:
                    user_uuid = await self._ensure_telegram_user_stub(conn, user_id)
                    if user_uuid is None:
                        continue
                    await self._upsert_chat_member_observation(
                        conn,
                        chat_uuid,
                        user_uuid,
                        role=role,
                        joined_at=observed_at if role == "member" else None,
                        last_seen_at=observed_at,
                    )
        except Exception as exc:
            logger.error("_on_chat_action error: %s", exc, exc_info=True)

    async def _on_user_update(self, worker: "TelegramWorker", event):
        try:
            user = await event.get_user()
            if user is not None:
                await self._upsert_user_full(user)
        except Exception as exc:
            logger.error("_on_user_update error: %s", exc, exc_info=True)

    async def _on_raw_reactions(self, worker: "TelegramWorker", update):
        """Handle UpdateMessageReactions / UpdateBotMessageReactions raw events.

        Phase 1 scope: write/update the per-message reaction *counts* into
        ``telegram_reaction_counts`` so dashboards can display engagement.
        Per-user reactor enumeration (Phase 2.2) calls
        ``GetMessageReactionsListRequest`` separately and writes
        ``telegram_reactions`` rows.

        Telethon raw payload shape:
          UpdateMessageReactions(peer, msg_id, top_msg_id, reactions)
          .reactions.results: list[ReactionCount]
              .reaction: ReactionEmoji(emoticon=str) | ReactionCustomEmoji(...)
              .count: int
        """
        try:
            from telethon.tl.types import (
                ReactionEmoji,
                ReactionCustomEmoji,
            )

            peer = getattr(update, "peer", None)
            msg_id = getattr(update, "msg_id", None)
            reactions = getattr(update, "reactions", None)
            if peer is None or msg_id is None or reactions is None:
                return

            # Resolve peer → platform_chat_id string.
            chat_pid: str | None = None
            for attr in ("channel_id", "chat_id", "user_id"):
                v = getattr(peer, attr, None)
                if v is not None:
                    chat_pid = str(v)
                    break
            if chat_pid is None:
                return

            # Build emoji -> count dict from the results list.
            counts: dict[str, int] = {}
            total = 0
            results = getattr(reactions, "results", None) or []
            for rc in results:
                emoji_obj = getattr(rc, "reaction", None)
                count = int(getattr(rc, "count", 0) or 0)
                if count <= 0:
                    continue
                if isinstance(emoji_obj, ReactionEmoji):
                    key = emoji_obj.emoticon
                elif isinstance(emoji_obj, ReactionCustomEmoji):
                    key = f"custom:{emoji_obj.document_id}"
                else:
                    key = str(emoji_obj)
                counts[key] = count
                total += count

            if not counts:
                return

            # Resolve message UUID via (chat_uuid, platform_message_id) lookup.
            # platform_message_id is namespaced as "{chat_pid}:{msg_id}" by _upsert_message.
            platform_message_id = f"{chat_pid}:{msg_id}"
            async with self.pool.acquire() as conn:
                msg_row = await conn.fetchrow(
                    "SELECT id FROM telegram_messages WHERE platform_message_id = $1",
                    platform_message_id,
                )
                if msg_row is None:
                    # Reaction arrived before we ingested the message — skip;
                    # the next backfill will refresh counts via this handler.
                    return
                msg_uuid = msg_row["id"]

                await conn.execute(
                    """
                    INSERT INTO telegram_reaction_counts
                        (message_id, counts, total_reactions, refreshed_at)
                    VALUES ($1, $2::jsonb, $3, NOW())
                    ON CONFLICT (message_id) DO UPDATE SET
                        counts = EXCLUDED.counts,
                        total_reactions = EXCLUDED.total_reactions,
                        refreshed_at = NOW()
                    """,
                    msg_uuid,
                    _tg_jsonb(counts),
                    total,
                )
        except Exception as exc:
            logger.debug("_on_raw_reactions failed: %s", exc)

    async def _write_realtime_message(
        self,
        worker: "TelegramWorker",
        message,
        chat_id: int,
        is_edit: bool = False,
    ):
        """INSERT (or UPDATE-on-edit) the message into telegram_messages."""
        # Resolve UUIDs via the existing chat upsert chain. We don't have the
        # entity here so just key off platform_chat_id.
        async with self.pool.acquire() as conn:
            chat_row = await conn.fetchrow(
                "SELECT id FROM telegram_chats WHERE platform_chat_id = $1",
                str(chat_id),
            )
            if chat_row is None:
                # The chat_id passed here doesn't always match telegram_chats'
                # platform_chat_id (raw peer id) format — some backfill/discussion
                # paths pass a different form, so the lookup missed and the message
                # landed with a NULL chat_id, orphaning it from the dashboard
                # (was ~8% / 101k rows). Resolve via the message's OWN peer (raw
                # id) and create a minimal chat row if it's genuinely new, so a
                # message is never orphaned. Existing rows are healed by
                # tmp/backfill_telegram_chat_id.py.
                peer = getattr(message, "peer_id", None)
                raw_pid = None
                if peer is not None:
                    raw_pid = (getattr(peer, "channel_id", None)
                               or getattr(peer, "chat_id", None)
                               or getattr(peer, "user_id", None))
                if raw_pid is not None:
                    ptype = type(peer).__name__
                    ctype = ("channel" if ptype == "PeerChannel"
                             else "group" if ptype == "PeerChat"
                             else "user" if ptype == "PeerUser" else None)
                    chat_row = await conn.fetchrow(
                        """
                        INSERT INTO telegram_chats (platform_chat_id, type)
                        VALUES ($1, $2)
                        ON CONFLICT (platform_chat_id) DO UPDATE
                            SET platform_chat_id = EXCLUDED.platform_chat_id
                        RETURNING id
                        """,
                        str(raw_pid), ctype,
                    )
            chat_uuid = chat_row["id"] if chat_row else None

            sender_uuid = None
            sender_id = getattr(message, "sender_id", None)
            if sender_id is not None:
                user_row = await conn.fetchrow(
                    "SELECT id FROM telegram_users WHERE platform_user_id = $1",
                    str(sender_id),
                )
                sender_uuid = user_row["id"] if user_row else None

            media_type = self._detect_message_type(message)
            platform_msg_id = f"{chat_id}:{message.id}"
            payload = _telethon_payload(message)
            payload_json = json.dumps(payload, default=_tg_json)
            reply_to, fwd_chat, fwd_msg, via_bot = self._msg_refs(message)
            # Tier 6: Telethon exposes message.pinned (bool) on Message objects;
            # backfill re-fetches also refresh it via the edit/upsert branches.
            is_pinned = bool(getattr(message, "pinned", False) or False)

            wrote_row = False
            if is_edit:
                # Update existing if present; else insert.
                await conn.execute("""
                    INSERT INTO telegram_messages (
                        platform_message_id, chat_id, sender_id, text, caption,
                        media_type, platform_created_at, metadata,
                        reply_to_message_id, forward_from_chat_id, forward_from_message_id, via_bot_id,
                        is_pinned
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    ON CONFLICT (platform_message_id) DO UPDATE SET
                        text = EXCLUDED.text,
                        caption = EXCLUDED.caption,
                        metadata = EXCLUDED.metadata,
                        is_pinned = EXCLUDED.is_pinned
                """,
                platform_msg_id, chat_uuid, sender_uuid,
                getattr(message, "message", None),
                getattr(message, "caption", None),
                media_type, message.date, payload_json,
                reply_to, fwd_chat, fwd_msg, via_bot,
                is_pinned,
                )
                wrote_row = True
            else:
                row = await conn.fetchrow("""
                    INSERT INTO telegram_messages (
                        platform_message_id, chat_id, sender_id, text, caption,
                        media_type, platform_created_at, metadata,
                        reply_to_message_id, forward_from_chat_id, forward_from_message_id, via_bot_id,
                        is_pinned
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    ON CONFLICT (platform_message_id) DO NOTHING
                    RETURNING id
                """,
                platform_msg_id, chat_uuid, sender_uuid,
                getattr(message, "message", None),
                getattr(message, "caption", None),
                media_type, message.date, payload_json,
                reply_to, fwd_chat, fwd_msg, via_bot,
                is_pinned,
                )
                wrote_row = row is not None

            message_uuid = await conn.fetchval(
                "SELECT id FROM telegram_messages WHERE platform_message_id = $1",
                platform_msg_id,
            )
            if message_uuid is not None:
                await self._capture_message_reaction_counts(conn, message_uuid, message)
                await self._capture_poll(conn, message_uuid, message)
                await self._enumerate_poll_votes_and_enqueue(
                    worker, message, str(chat_id), message_uuid, conn=conn, chat_uuid=chat_uuid
                )
            # Tier 6: venue/event extraction (best effort — never breaks the
            # hot realtime path; helper swallows all exceptions internally).
            await self._extract_message_event(message, chat_uuid, platform_msg_id, conn=conn)
            await self._record_message_membership_signals(
                conn, message, chat_uuid, sender_uuid
            )
            await persist_discovered_links(
                conn,
                source="telegram",
                source_table="telegram_messages",
                source_record_id=platform_msg_id,
                context_id=str(chat_id),
                entity_id=str(sender_id or ""),
                text=" ".join(
                    v for v in (
                        getattr(message, "message", None),
                        getattr(message, "caption", None),
                    )
                    if v
                ),
                metadata={
                    "platform_message_id": platform_msg_id,
                    "platform_chat_id": str(chat_id),
                    "platform_sender_id": str(sender_id or ""),
                    "ingest_path": self.INGEST_PATH,
                    "raw_payload_kind": "message_edit" if is_edit else "message",
                },
            )
        if wrote_row:
            self._progress_count += 1
        self._archive_raw_payload(
            artifact_id=f"messages/{platform_msg_id}",
            payload=payload,
            target_tables=["telegram_messages"],
            metadata={
                "platform_message_id": platform_msg_id,
                "platform_chat_id": str(chat_id),
                "platform_sender_id": str(getattr(message, "sender_id", "") or ""),
                "ingest_path": self.INGEST_PATH,
                "raw_payload_kind": "message_edit" if is_edit else "message",
            },
        )

    async def _extract_message_event(self, message, chat_uuid, platform_msg_id, conn=None):
        """Tier 6 (best effort): extract venue/event info → telegram_events.

        Handles:
          - MessageMediaVenue → event_type 'venue' (title/address/venue_type).
            Lat/lng are deliberately NOT captured here — geo extraction lives
            in telegram_message_locations (Tier 5, separate agent/path).
          - MessageActionPinMessage service messages → event_type 'pin'.

        Isolated try/except: must NEVER break the message write path. Reuses
        the caller's connection when given, else acquires one from the pool.
        """
        try:
            event_type = title = address = venue_type = None
            starts_at = None

            media = getattr(message, "media", None)
            if media is not None and type(media).__name__ == "MessageMediaVenue":
                event_type = "venue"
                title = getattr(media, "title", None)
                address = getattr(media, "address", None)
                venue_type = getattr(media, "venue_type", None)
            else:
                action = getattr(message, "action", None)
                if action is not None and type(action).__name__ == "MessageActionPinMessage":
                    event_type = "pin"
                    starts_at = getattr(message, "date", None)

            if event_type is None:
                return

            sql = """
                INSERT INTO telegram_events (
                    platform_message_id, chat_id, event_type,
                    title, address, venue_type, starts_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (platform_message_id) DO NOTHING
            """
            args = (platform_msg_id, chat_uuid, event_type,
                    title, address, venue_type, starts_at)
            if conn is not None:
                await conn.execute(sql, *args)
            else:
                async with self.pool.acquire() as _conn:
                    await _conn.execute(sql, *args)
        except Exception as exc:
            logger.debug("_extract_message_event failed for %s: %s", platform_msg_id, exc)

    @staticmethod
    def _coerce_telegram_user_ids(value) -> list[int]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            values = value
        else:
            values = [value]
        out: list[int] = []
        for item in values:
            user_id = (
                getattr(item, "user_id", None)
                or getattr(item, "id", None)
                or item
            )
            try:
                out.append(int(user_id))
            except (TypeError, ValueError):
                continue
        return out

    async def _ensure_telegram_user_stub(self, conn, platform_user_id) -> str | None:
        if platform_user_id is None:
            return None
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO telegram_users (platform_user_id, updated_at)
                VALUES ($1, NOW())
                ON CONFLICT (platform_user_id) DO NOTHING
                RETURNING id
                """,
                str(platform_user_id),
            )
            if row is not None:
                return row["id"]
            return await conn.fetchval(
                "SELECT id FROM telegram_users WHERE platform_user_id = $1",
                str(platform_user_id),
            )
        except Exception as exc:
            logger.debug("telegram user stub upsert failed for %s: %s", platform_user_id, exc)
            return None

    async def _upsert_chat_member_observation(
        self,
        conn,
        chat_uuid,
        user_uuid,
        *,
        role: str = "member",
        joined_at=None,
        last_seen_at=None,
    ) -> None:
        if chat_uuid is None or user_uuid is None:
            return
        await conn.execute(
            """
            INSERT INTO telegram_chat_members
                (chat_id, user_id, role, joined_at, last_seen_at, refreshed_at)
            VALUES ($1, $2, $3, $4, COALESCE($5, NOW()), NOW())
            ON CONFLICT (chat_id, user_id) DO UPDATE SET
                role = CASE
                    WHEN COALESCE(EXCLUDED.last_seen_at, NOW())
                       < COALESCE(telegram_chat_members.last_seen_at, '-infinity'::timestamptz)
                        THEN telegram_chat_members.role
                    WHEN EXCLUDED.role = 'member'
                     AND telegram_chat_members.role IN ('creator', 'admin')
                        THEN telegram_chat_members.role
                    ELSE EXCLUDED.role
                END,
                joined_at = COALESCE(telegram_chat_members.joined_at, EXCLUDED.joined_at),
                last_seen_at = GREATEST(
                    COALESCE(telegram_chat_members.last_seen_at, '-infinity'::timestamptz),
                    COALESCE(EXCLUDED.last_seen_at, NOW())
                ),
                refreshed_at = NOW()
            """,
            chat_uuid,
            user_uuid,
            role,
            joined_at,
            last_seen_at,
        )

    async def _record_message_membership_signals(
        self,
        conn,
        message,
        chat_uuid,
        sender_uuid,
    ) -> None:
        """Normalize weak member evidence from messages and service actions."""
        if chat_uuid is None:
            return
        try:
            message_at = getattr(message, "date", None)
            sender_id = getattr(message, "sender_id", None)
            if sender_uuid is None and sender_id is not None:
                sender_uuid = await self._ensure_telegram_user_stub(conn, sender_id)
            if sender_uuid is not None:
                await self._upsert_chat_member_observation(
                    conn,
                    chat_uuid,
                    sender_uuid,
                    role="member",
                    last_seen_at=message_at,
                )

            action = getattr(message, "action", None)
            if action is None:
                return

            action_name = type(action).__name__
            member_ids: list[int] = []
            left_ids: list[int] = []
            member_ids.extend(self._coerce_telegram_user_ids(getattr(action, "users", None)))
            member_ids.extend(self._coerce_telegram_user_ids(getattr(action, "user_id", None)))

            if action_name == "MessageActionChatDeleteUser":
                left_ids.extend(member_ids)
                member_ids = []
            elif action_name in {
                "MessageActionChatJoinedByLink",
                "MessageActionChatJoinedByRequest",
            }:
                member_ids.extend(self._coerce_telegram_user_ids(sender_id))
            elif action_name == "MessageActionChatCreate":
                member_ids.extend(self._coerce_telegram_user_ids(sender_id))

            actor_ids = self._coerce_telegram_user_ids(getattr(action, "inviter_id", None))
            actor_ids.extend(self._coerce_telegram_user_ids(getattr(action, "from_id", None)))

            for user_id in dict.fromkeys(member_ids):
                user_uuid = await self._ensure_telegram_user_stub(conn, user_id)
                await self._upsert_chat_member_observation(
                    conn,
                    chat_uuid,
                    user_uuid,
                    role="member",
                    joined_at=message_at,
                    last_seen_at=message_at,
                )
            for user_id in dict.fromkeys(actor_ids):
                user_uuid = await self._ensure_telegram_user_stub(conn, user_id)
                await self._upsert_chat_member_observation(
                    conn,
                    chat_uuid,
                    user_uuid,
                    role="member",
                    last_seen_at=message_at,
                )
            for user_id in dict.fromkeys(left_ids):
                user_uuid = await self._ensure_telegram_user_stub(conn, user_id)
                await self._upsert_chat_member_observation(
                    conn,
                    chat_uuid,
                    user_uuid,
                    role="left",
                    last_seen_at=message_at,
                )
        except Exception as exc:
            logger.debug("_record_message_membership_signals failed: %s", exc)

    async def _upsert_user_full(self, user):
        """Upsert with full Telethon user attributes (bot/verified/premium/etc).

        Also detects field-level changes vs the previous DB row and writes them
        to telegram_user_changes (UserChangeTracker). This is non-fatal — if
        change-tracking fails, the upsert still proceeds.
        """
        try:
            new_row = {
                "username": getattr(user, "username", None),
                "first_name": getattr(user, "first_name", None),
                "last_name": getattr(user, "last_name", None),
                "phone": getattr(user, "phone", None),
                "bio": getattr(user, "bio", None) or getattr(user, "about", None),
                "is_bot": getattr(user, "bot", None),  # SYNC #40
            }
            async with self.pool.acquire() as conn:
                # Read current row BEFORE upserting so diff is honest.
                current = await conn.fetchrow(
                    "SELECT username, first_name, last_name, phone, bio "
                    "FROM telegram_users WHERE platform_user_id = $1",
                    str(user.id),
                )
                # Detect-and-log changes (best effort — never break ingestion).
                if self._user_change_tracker is not None and current is not None:
                    try:
                        await self._user_change_tracker.detect_and_log(
                            table="telegram_user_changes",
                            pk_col="user_id",
                            pk_val=int(user.id),
                            current_row=dict(current),
                            new_row=new_row,
                            fields=("username", "first_name", "last_name", "phone", "bio"),
                        )
                    except Exception as exc:
                        logger.debug("user_change_tracker.detect_and_log failed: %s", exc)
                # Upsert full row.
                await conn.execute("""
                    INSERT INTO telegram_users (
                        platform_user_id, username, first_name, last_name, phone, bio, is_bot, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                    ON CONFLICT (platform_user_id) DO UPDATE SET
                        username = COALESCE(EXCLUDED.username, telegram_users.username),
                        first_name = COALESCE(EXCLUDED.first_name, telegram_users.first_name),
                        last_name = COALESCE(EXCLUDED.last_name, telegram_users.last_name),
                        phone = COALESCE(EXCLUDED.phone, telegram_users.phone),
                        bio = COALESCE(EXCLUDED.bio, telegram_users.bio),
                        is_bot = COALESCE(EXCLUDED.is_bot, telegram_users.is_bot),
                        updated_at = NOW()
                """,
                str(user.id),
                new_row["username"],
                new_row["first_name"],
                new_row["last_name"],
                new_row["phone"],
                new_row["bio"],
                new_row["is_bot"],
                )
            self._archive_raw_payload(
                artifact_id=f"users/{user.id}",
                payload=_telethon_payload(user),
                target_tables=["telegram_users"],
                metadata={
                    "platform_user_id": str(user.id),
                    "username": getattr(user, "username", None),
                    "ingest_path": self.INGEST_PATH,
                    "raw_payload_kind": "user_profile",
                },
            )
        except Exception as exc:
            logger.debug("_upsert_user_full failed for %s: %s", getattr(user, "id", "?"), exc)

    def _detect_message_type(self, message) -> str:
        """Return message_type string — ported from realtime_worker."""
        return _parse_detect_message_type(message)

    def _extract_file_info(self, message) -> tuple:
        """Return (file_unique_id, None, ext) — ported from realtime_worker.

        file_unique_id derives from the Telethon-native object ID
        (photo.id or document.id), which is stable + unique across
        Telegram. Returns (None, None, None) if the message has no
        downloadable media.
        """
        return _parse_extract_file_info(message)

    # ==================================================================
    # Backfill — ported from
    # telegramcollector/services/collector/backfill_worker.py
    # ==================================================================

    async def backfill_chat(
        self,
        chat_id,
        target_depth: int | None = None,
        max_iterations: int = 10000,
        worker: "TelegramWorker | None" = None,
    ):
        """Cursor-based historical backfill of messages in a chat.

        Walks newest -> oldest via Telethon ``iter_messages`` with ``max_id``
        pagination. Each batch of <=batch_size is persisted before advancing
        the cursor; FloodWait is absorbed via _handle_flood_wait. Bounded by
        ``target_depth`` (stop after N messages persisted) and
        ``max_iterations`` (safety: stop after M batches even if Telegram
        keeps streaming).

        Returns the count of messages written.
        """
        # Auto-spawn a worker if not given one.
        if worker is None:
            if not self._workers:
                self._workers = await self._spawn_workers()
            if not self._workers:
                logger.error("backfill_chat: no Telegram workers — bailing")
                return 0
            worker = self._workers[0]

        client = worker.client
        try:
            entity = await client.get_entity(int(chat_id))
        except (ValueError, TypeError):
            entity = await client.get_entity(chat_id)
        await self._upsert_chat(entity)

        chat_id_int = int(getattr(entity, "id"))
        chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or str(chat_id_int)
        batch_size = self._batch_size
        max_id = 0  # 0 means "from newest"
        written = 0
        iterations = 0

        logger.info(
            "backfill_chat: chat_id=%s name=%s target_depth=%s max_iter=%d",
            chat_id_int, chat_name, target_depth, max_iterations,
        )

        while iterations < max_iterations:
            if self._stop.is_set():
                break
            iterations += 1

            try:
                messages = []
                async for msg in client.iter_messages(
                    entity, limit=batch_size,
                    max_id=max_id if max_id > 0 else 0,
                    reverse=False,
                ):
                    messages.append(msg)
            except Exception as exc:
                if _is_flood_wait(exc):
                    await self._handle_flood_wait(worker, exc)
                    continue
                logger.error("backfill_chat: fetch failed: %s", exc)
                break

            if not messages:
                logger.info("backfill_chat: chat=%s reached end (no more messages)", chat_id_int)
                break

            for message in messages:
                try:
                    sender_uuid = None
                    if getattr(message, "sender_id", None):
                        sender_uuid = await self._upsert_sender(worker, message.sender_id)
                    await self._upsert_message(message, str(chat_id_int), sender_uuid, worker=worker)
                    written += 1
                except Exception as exc:
                    logger.warning(
                        "backfill_chat: failed write chat=%s msg=%s: %s",
                        chat_id_int, getattr(message, "id", "?"), exc,
                    )

            # Advance cursor — min ID in this batch is the next max_id.
            batch_ids = [m.id for m in messages if hasattr(m, "id")]
            if batch_ids:
                max_id = min(batch_ids)

            if target_depth is not None and written >= target_depth:
                logger.info(
                    "backfill_chat: chat=%s hit target_depth=%d (written=%d)",
                    chat_id_int, target_depth, written,
                )
                break

            if len(messages) < batch_size:
                # Partial batch → end of channel.
                break

        logger.info(
            "backfill_chat: chat=%s complete written=%d iterations=%d",
            chat_id_int, written, iterations,
        )
        return written

    # ==================================================================
    # Dialog enumeration — cherry-picked from
    # telegramtoolkit/src/core/scan_targets.py (iter_dialogs pattern)
    # ==================================================================

    async def collect_dialogs(self) -> list[dict]:
        """Enumerate joined dialogs across all workers and upsert telegram_chats.

        Returns a deduplicated list of {platform_chat_id, title, type} dicts.
        Workers running in parallel will see the same shared chats; we
        dedupe by platform_chat_id so we only INSERT each one once.
        """
        if not self._workers:
            self._workers = await self._spawn_workers()
        if not self._workers:
            logger.error("collect_dialogs: no Telegram workers — bailing")
            return []

        seen: dict[str, dict] = {}
        for worker in self._workers:
            if self._stop.is_set():
                break
            try:
                async for dialog in worker.client.iter_dialogs():
                    entity = getattr(dialog, "entity", None)
                    if entity is None:
                        continue
                    cid = str(getattr(entity, "id", ""))
                    if not cid or cid in seen:
                        continue
                    # Upsert into telegram_chats.
                    try:
                        await self._upsert_chat(entity)
                    except Exception as exc:
                        logger.debug("upsert_chat failed for %s: %s", cid, exc)
                    if getattr(entity, "broadcast", False):
                        chat_type = "channel"
                    elif getattr(entity, "megagroup", False):
                        chat_type = "supergroup"
                    elif hasattr(entity, "title"):
                        chat_type = "group"
                    else:
                        chat_type = "private"
                    seen[cid] = {
                        "platform_chat_id": cid,
                        "title": getattr(entity, "title", None)
                                 or getattr(entity, "username", None)
                                 or cid,
                        "type": chat_type,
                    }
            except Exception as exc:
                logger.error(
                    "[worker=%d account=%s] collect_dialogs failed: %s",
                    worker.worker_id, worker.account.name, exc,
                )

        logger.info("collect_dialogs: %d unique dialog(s)", len(seen))
        return list(seen.values())

    # ==================================================================
    # Common-chat / chat-members refresh — daily 03:00 SGT cron
    # ==================================================================

    async def collect_chat_members(self, chat_id, worker: "TelegramWorker | None" = None) -> int:
        """Iterate participants of chat_id and upsert into telegram_chat_members.

        Per-memory PRD: refreshed daily at 03:00 SGT for common-chat-membership
        analytics. Sets refreshed_at = NOW() so stale rows can be pruned.

        Schema (post-Phase-1): chat_id and user_id are UUIDs that FK back to
        telegram_chats(id) and telegram_users(id). We resolve from the platform
        bigint IDs to UUIDs by querying after _upsert_chat / _upsert_user_full.

        Returns the number of upserted member rows.
        """
        if worker is None:
            if not self._workers:
                self._workers = await self._spawn_workers()
            if not self._workers:
                logger.error("collect_chat_members: no workers — bailing")
                return 0
            worker = self._workers[0]

        client = worker.client
        try:
            entity = await client.get_entity(int(chat_id))
        except (ValueError, TypeError):
            entity = await client.get_entity(chat_id)

        chat_platform_id = str(getattr(entity, "id"))
        await self._upsert_chat(entity)

        # Resolve chat UUID once.
        async with self.pool.acquire() as conn:
            chat_row = await conn.fetchrow(
                "SELECT id FROM telegram_chats WHERE platform_chat_id = $1",
                chat_platform_id,
            )
        if chat_row is None:
            logger.error(
                "collect_chat_members: chat %s not in DB after upsert — bailing",
                chat_platform_id,
            )
            return 0
        chat_uuid = chat_row["id"]

        seen: set[int] = set()
        upserted = 0
        try:
            async for participant in client.iter_participants(entity):
                if self._stop.is_set():
                    break
                pid = getattr(participant, "id", None)
                if pid is None or pid in seen:
                    continue
                seen.add(pid)

                # Best-effort upsert into telegram_users so the FK target exists.
                try:
                    await self._upsert_user_full(participant)
                except Exception:
                    pass

                # Resolve user UUID — _upsert_user_full just guaranteed the row exists.
                async with self.pool.acquire() as conn:
                    user_row = await conn.fetchrow(
                        "SELECT id FROM telegram_users WHERE platform_user_id = $1",
                        str(pid),
                    )
                if user_row is None:
                    continue
                user_uuid = user_row["id"]

                # Determine role from participant.participant.* attributes.
                role = "member"
                p = getattr(participant, "participant", None)
                if p is not None:
                    pname = type(p).__name__
                    if "Creator" in pname:
                        role = "creator"
                    elif "Admin" in pname:
                        role = "admin"
                    elif "Banned" in pname:
                        role = "banned"
                    elif "Left" in pname:
                        role = "left"

                joined_at = None
                if p is not None:
                    joined_at = getattr(p, "date", None)

                async with self.pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO telegram_chat_members
                            (chat_id, user_id, role, joined_at, last_seen_at, refreshed_at)
                        VALUES ($1, $2, $3, $4, NOW(), NOW())
                        ON CONFLICT (chat_id, user_id) DO UPDATE SET
                            role = EXCLUDED.role,
                            joined_at = COALESCE(EXCLUDED.joined_at, telegram_chat_members.joined_at),
                            last_seen_at = NOW(),
                            refreshed_at = NOW()
                    """, chat_uuid, user_uuid, role, joined_at)
                upserted += 1
        except Exception as exc:
            if _is_flood_wait(exc):
                await self._handle_flood_wait(worker, exc)
            else:
                logger.error(
                    "collect_chat_members chat=%s failed: %s",
                    chat_platform_id, exc,
                )

        logger.info(
            "collect_chat_members: chat=%s upserted=%d",
            chat_platform_id, upserted,
        )
        return upserted

    # ==================================================================
    # User profile + photos — cherry-picked from
    # telegramtoolkit/src/managers/download_profile_photos.py
    # ==================================================================

    async def collect_user_profile(
        self, user_id, worker: "TelegramWorker | None" = None,
    ) -> dict | None:
        """Fetch user metadata + profile photos.

        Returns a dict of the persisted fields, or None if the user can't
        be resolved by any worker.
        """
        if worker is None:
            if not self._workers:
                self._workers = await self._spawn_workers()
            if not self._workers:
                logger.error("collect_user_profile: no workers — bailing")
                return None
            worker = self._workers[0]

        # Resolve via whichever account actually owns this user (cross-account).
        # A single-worker get_entity raised "Cannot find any entity" for users
        # owned by the other N-1 accounts (the profile-path equivalent of the
        # stories bug) — that was the ~75/20min WARNING spam AND lost profiles.
        # Route the rest of the collection through the owning account.
        try:
            owner_w, user = await self._resolve_entity_any_worker(worker, str(user_id))
            worker = owner_w
            client = owner_w.client
        except EntityUnresolvable:
            logger.debug("collect_user_profile: no connected account owns user %s", user_id)
            return None
        except Exception as exc:
            logger.debug("collect_user_profile resolve (transient) for %s: %s", user_id, exc)
            return None

        # ── User-intelligence diff: snapshot the row BEFORE upserting so the
        # change tracker can compare old → new and emit one row per changed
        # field into telegram_user_changes. Wrapped in try/except so any
        # failure (DB, schema drift, etc.) is non-fatal to ingestion.
        prev_row = None
        try:
            async with self.pool.acquire() as conn:
                prev_row = await conn.fetchrow(
                    "SELECT username, first_name, last_name "
                    "FROM telegram_users WHERE platform_user_id = $1",
                    str(getattr(user, "id", user_id)),
                )
        except Exception as exc:
            logger.debug("user_change_tracker: prev-row fetch failed: %s", exc)

        await self._upsert_user_full(user)

        try:
            tracker = UserChangeTracker(self.pool)
            new_snapshot = {
                "username":   getattr(user, "username", None),
                "first_name": getattr(user, "first_name", None),
                "last_name":  getattr(user, "last_name", None),
                "bio":        getattr(user, "about", None) or getattr(user, "bio", None),
                "premium":    getattr(user, "premium", None),
                "verified":   getattr(user, "verified", None),
                "phone":      getattr(user, "phone", None),
            }
            photo = getattr(user, "photo", None)
            if photo is not None:
                new_snapshot["profile_photo_id"] = getattr(photo, "photo_id", None)
            await tracker.detect_and_log(
                table="telegram_user_changes",
                pk_col="user_id",
                pk_val=int(getattr(user, "id", 0) or 0),
                current_row=dict(prev_row) if prev_row else None,
                new_row=new_snapshot,
                fields=TELEGRAM_TRACKED_FIELDS,
            )
        except Exception as exc:
            logger.debug("user_change_tracker: detect_and_log failed: %s", exc)

        uid = str(getattr(user, "id", user_id))
        uname = (getattr(user, "username", None)
                 or getattr(user, "first_name", None) or uid)

        # Profile photo (first/largest) — download the file AND record its path in
        # telegram_users.photo_url so the user registry/dashboard can show it.
        try:
            cid = f"profile_user_{uid}"
            if not self.is_known(cid):
                photo_bytes = await client.download_profile_photo(user, bytes)
                if photo_bytes:
                    await self.download_media({
                        "entity_id": uid,
                        "entity_name": uname,
                        "content_type": "user_profile_photo",
                        "content_id": cid,
                        "data": photo_bytes,
                        "extension": "jpg",
                        # Deep-link URL — public users have https://t.me/<username>
                        "chat_username": getattr(user, "username", None),
                    }, worker=worker)
            # set photo_url to the stored file (whether just downloaded or already known)
            async with self.pool.acquire() as conn:
                fp = await conn.fetchval(
                    "SELECT file_path FROM media_items WHERE source='telegram' AND content_id=$1", cid
                )
                await conn.execute(
                    "UPDATE telegram_users SET photo_url=COALESCE($1, photo_url), updated_at=NOW() WHERE platform_user_id=$2",
                    fp, uid,
                )
        except Exception as exc:
            logger.debug("user profile photo failed for %s: %s", uid, exc)

        # Older photos via get_profile_photos (cherry-pick from toolkit).
        try:
            photos = await client.get_profile_photos(user)
            for idx, photo in enumerate(photos or []):
                pid = getattr(photo, "id", None)
                if pid is None:
                    continue
                cid_p = f"profile_user_{uid}_{pid}"
                if self.is_known(cid_p):
                    continue
                try:
                    photo_bytes = await client.download_media(photo, bytes)
                    if photo_bytes:
                        await self.download_media({
                            "entity_id": uid,
                            "entity_name": uname,
                            "content_type": "user_profile_photo",
                            "content_id": cid_p,
                            "data": photo_bytes,
                            "extension": "jpg",
                            "chat_username": getattr(user, "username", None),
                        }, worker=worker)
                except Exception as exc:
                    logger.debug("photo %s for %s failed: %s", pid, uid, exc)
        except Exception as exc:
            logger.debug("get_profile_photos failed for %s: %s", uid, exc)

        return {
            "platform_user_id": uid,
            "username": getattr(user, "username", None),
            "first_name": getattr(user, "first_name", None),
            "last_name": getattr(user, "last_name", None),
            "phone": getattr(user, "phone", None),
            "is_bot": bool(getattr(user, "bot", False)),
            "is_verified": bool(getattr(user, "verified", False)),
            "is_premium": bool(getattr(user, "premium", False)),
        }

    async def _collect_user_photos_pass(self, worker: "TelegramWorker", batch: int = 15) -> int:
        """Per-cycle bounded sweep: download profile photos for telegram_users that
        don't have one yet + set photo_url. Best-effort (cross-account users that
        worker[0] can't resolve are retried on later cycles)."""
        if not self.pool:
            return 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT platform_user_id FROM telegram_users "
                "WHERE photo_url IS NULL AND (is_deleted IS NOT TRUE) "
                "ORDER BY updated_at ASC NULLS FIRST LIMIT $1",
                batch,
            )
        n = 0
        for r in rows:
            if self._stop.is_set():
                break
            try:
                await self.collect_user_profile(r["platform_user_id"], worker=worker)
                n += 1
            except Exception as exc:
                logger.debug("user photo collect failed %s: %s", r["platform_user_id"], exc)
        return n

    # ==================================================================
    # Single-message media download — routed through src.core.media_download
    # ==================================================================

    async def download_message_media(
        self,
        message_or_id,
        worker: "TelegramWorker | None" = None,
        chat_id=None,
    ):
        """Download the media attached to a Telethon message.

        Accepts either a Telethon Message object directly, or
        (message_id, chat_id) so callers without an event handle can
        re-fetch. Routes through self.download_media (which performs
        atomic write + sha256 + insert_media_item) — that is the unified
        delegated-backend equivalent of src/core/media_download.py for
        Telethon's library-level download_media() API.
        """
        if worker is None:
            if not self._workers:
                self._workers = await self._spawn_workers()
            if not self._workers:
                logger.error("download_message_media: no workers — bailing")
                return None
            worker = self._workers[0]

        client = worker.client

        # Resolve message object if only an ID was passed.
        message = message_or_id
        if not hasattr(message, "media"):
            if chat_id is None:
                logger.error("download_message_media: chat_id required when given an ID")
                return None
            try:
                msgs = await client.get_messages(int(chat_id), ids=int(message_or_id))
                message = msgs if hasattr(msgs, "media") else (msgs[0] if msgs else None)
            except Exception as exc:
                logger.error("download_message_media: get_messages failed: %s", exc)
                return None

        if message is None or getattr(message, "media", None) is None:
            return None

        chat_id_str = str(chat_id) if chat_id is not None else str(getattr(message, "chat_id", "unknown"))
        # Try to resolve a name; fall back to chat_id.
        chat_name = chat_id_str
        chat_username: str | None = None
        try:
            entity = await client.get_entity(int(chat_id_str))
            chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or chat_id_str
            chat_username = getattr(entity, "username", None)
        except Exception:
            pass

        from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
        if isinstance(message.media, MessageMediaPhoto):
            await self._handle_photo(worker, message, chat_id_str, chat_name, chat_username)
            return True
        if isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            mime = getattr(doc, "mime_type", "") or ""
            await self._handle_document(worker, message, chat_id_str, chat_name, mime, chat_username)
            return True

        # Unknown media type — fall through to a generic Telethon download.
        try:
            data = await client.download_media(message.media, bytes)
            if not data:
                return None
            _, _, ext = self._extract_file_info(message)
            await self.download_media({
                "entity_id": chat_id_str,
                "entity_name": chat_name,
                "content_type": "media",
                "content_id": str(message.id),
                "data": data,
                "extension": ext or "bin",
                "raw": message.to_dict(),
                "chat_username": chat_username,
                "message_id": message.id,
            }, worker=worker)
            return True
        except Exception as exc:
            logger.error("download_message_media generic path failed: %s", exc)
            return None

    # ==================================================================
    # Cleanup
    # ==================================================================

    async def cleanup(self):
        self._realtime_running = False
        try:
            await self._hub_notifier.stop()
        except Exception:
            pass
        try:
            await self._bot_pool.stop_health_monitor()
        except Exception:
            pass
        for w in self._workers:
            await w.disconnect()
        self._workers = []
        self._primary_client = None
