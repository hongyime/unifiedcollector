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
import tempfile
import time
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
from src.core.file_naming import sanitize_name
from src.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from src.core.user_change_tracker import (
    UserChangeTracker,
    TELEGRAM_TRACKED_FIELDS,
)

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


_MIME_EXT_MAP = _parse_MIME_EXT_MAP


def _ext_from_mime(mime_type):
    return _parse_ext_from_mime(mime_type)


def _is_flood_wait(exc):
    """Detect FloodWaitError without importing telethon at module scope."""
    name = type(exc).__name__
    if name == "FloodWaitError":
        return True
    return hasattr(exc, "seconds") and "flood" in name.lower()


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
        self._hub_group_id: int | None = None

        # User change tracker — wires telegram_user_changes writes into _upsert_user_full.
        # Lazy: created on first DB-bound call (since pool is set up by BaseCollector at startup).
        self._user_change_tracker: UserChangeTracker | None = None

        # Reaction-list per-message cap (Q2 decision: per-emoji per-message).
        self._reaction_user_cap = int(os.getenv("TELEGRAM_REACTION_USER_CAP", "500"))

        # Discussion-group dwell range — random jitter to look human (Q3 always-leave).
        self._discussion_dwell_min = int(os.getenv("TELEGRAM_DISCUSSION_DWELL_MIN", "60"))
        self._discussion_dwell_max = int(os.getenv("TELEGRAM_DISCUSSION_DWELL_MAX", "180"))

        # Hot-reload task (item 4.6) — listens for new accounts via NOTIFY.
        self._hot_reload_task: asyncio.Task | None = None

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

        # Auto-backfill new accounts (item 2.4).
        # For each connected worker, check if their account name has been seen
        # before. If not, run full collect_dialogs() to discover all their chats
        # and queue them for backfill.
        auto_backfill_enabled = (
            os.getenv("TELEGRAM_AUTO_BACKFILL_NEW_ACCOUNTS", "true").lower() == "true"
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

        # Spider queue: fan out across allowed workers for parallelism.
        # TELEGRAM_SPIDER_ACCOUNTS restricts which accounts can spider.
        if os.getenv("TELEGRAM_SPIDER_ENABLED", "true").lower() == "true":
            try:
                allowed = self._spider_accounts
                spider_workers = [
                    w for w in self._workers
                    if not allowed or w.account.name.lower() in allowed
                ]
                if allowed and len(spider_workers) < len(self._workers):
                    logger.info("Spider restricted to accounts: %s (%d/%d workers)",
                                ", ".join(allowed), len(spider_workers), len(self._workers))
                spider_tasks = [
                    self._process_spider_queue(w)
                    for w in spider_workers
                ]
                results = await asyncio.gather(*spider_tasks, return_exceptions=True)
                for w, r in zip(self._workers, results):
                    if isinstance(r, Exception):
                        logger.error("Spider queue worker=%d crashed: %s", w.worker_id, r)
            except Exception as e:
                logger.error("Spider queue processing failed: %s", e)

        if self._story_enabled:
            try:
                await self._scan_stories(self._workers[0], targets)
            except Exception as e:
                logger.error("Story scan failed: %s", e)

        if self._group_join_enabled:
            try:
                await self._process_join_queue()
            except Exception as e:
                logger.error("Join queue failed: %s", e)

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
        processed = 0
        while not self._stop.is_set() and processed < max_per_cycle:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    UPDATE telegram_spider_queue
                    SET status = 'processing'
                    WHERE id = (
                        SELECT id FROM telegram_spider_queue
                        WHERE status = 'pending'
                        ORDER BY priority ASC, collected_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
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
            except Exception as exc:
                logger.error(
                    "[spider w=%d] failed chat %s: %s", worker.worker_id, chat_id, exc,
                )
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE telegram_spider_queue SET status = 'failed' WHERE platform_chat_id = $1",
                        chat_id,
                    )
        logger.info("[spider w=%d] finished: processed %d chats", worker.worker_id, processed)

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
        # Sleep at least until the flood-wait elapses (capped to 5min so we
        # don't block the worker on truly long bans — those are surfaced by
        # is_available() and the next cycle skips this acct).
        await asyncio.sleep(min(wait_seconds, 300))
        worker.state = SessionState.CONNECTED

    # ------------------------------------------------------------------
    # Per-chat collection (now takes a worker arg)
    # ------------------------------------------------------------------

    async def _collect_chat(self, worker: "TelegramWorker", target: str):
        from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

        client = worker.client
        try:
            entity = await client.get_entity(int(target))
        except ValueError:
            entity = await client.get_entity(target)

        chat_id = str(entity.id)
        chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or chat_id

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

            await self._upsert_message(message, chat_id, sender_uuid)

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
                    await self._handle_photo(worker, message, chat_id, chat_name)
                    count += 1
                elif isinstance(message.media, MessageMediaDocument):
                    doc = message.media.document
                    if doc and (getattr(doc, "size", 0) or 0) <= self._max_media_size:
                        mime = getattr(doc, "mime_type", "") or ""
                        # Tier 3: route ALL documents through the classifier
                        # (was image/video-only, which dropped PDFs/office/audio).
                        # The classifier whitelists safe docs + audio + static
                        # stickers and skips executables/code/animated stickers.
                        if await self._handle_document(worker, message, chat_id, chat_name, mime):
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
          5. Run `collect_chat_members()` + `backfill_chat(limit=2000)`.
          6. Call `LeaveChannelRequest` (always leave per Bryan's requirement).
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
        joined_at = None

        try:
            if not already_member:
                logger.info(
                    "[worker=%d] Joining discussion group %s (%s) for channel %s",
                    worker.worker_id, discussion_title, discussion_platform_id, channel_platform_id,
                )
                await client(JoinChannelRequest(discussion_entity))
                joined_at = asyncio.get_event_loop().time()

                # Human-like dwell before scraping.
                dwell = random.randint(self._discussion_dwell_min, self._discussion_dwell_max)
                logger.debug("Dwelling %ds before scraping discussion group", dwell)
                await asyncio.sleep(dwell)

            # Scrape members.
            members_collected = await self.collect_chat_members(
                discussion_entity.id, worker=worker
            )

            # Scrape recent messages (limit 2000).
            msg_count = 0
            async for message in client.iter_messages(discussion_entity, limit=2000):
                if self._stop.is_set():
                    abort_reason = "stop_signal"
                    break
                sender_uuid = None
                if message.sender_id:
                    sender_uuid = await self._upsert_sender(worker, message.sender_id)
                await self._upsert_message(message, discussion_platform_id, sender_uuid)
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
            # Always leave (per Bryan's "always leave" requirement).
            try:
                logger.info(
                    "[worker=%d] Leaving discussion group %s",
                    worker.worker_id, discussion_title,
                )
                await client(LeaveChannelRequest(discussion_entity))
            except Exception as leave_exc:
                logger.debug("LeaveChannelRequest failed: %s", leave_exc)

            # Record the visit.
            if discussion_uuid is not None:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO telegram_discussion_visits (
                            channel_chat_id, discussion_chat_id,
                            joined_at, left_at,
                            members_collected, messages_collected, abort_reason
                        ) VALUES ($1, $2, NOW(), NOW(), $3, $4, $5)
                        """,
                        channel_uuid,
                        discussion_uuid,
                        members_collected,
                        messages_collected,
                        abort_reason,
                    )

        logger.info(
            "[worker=%d] Discussion spider done: channel=%s discussion=%s members=%d msgs=%d abort=%s",
            worker.worker_id, channel_platform_id, discussion_platform_id,
            members_collected, messages_collected, abort_reason,
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
                    INSERT INTO telegram_users (platform_user_id, username, first_name, last_name, phone, is_deleted, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, NOW())
                    ON CONFLICT (platform_user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        phone = COALESCE(EXCLUDED.phone, telegram_users.phone),
                        is_deleted = EXCLUDED.is_deleted,
                        updated_at = NOW()
                    RETURNING id
                """,
                str(user.id), user.username, user.first_name, user.last_name,
                getattr(user, "phone", None),
                getattr(user, "deleted", False),
                )
                return row['id']
        except Exception as e:
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

    async def _upsert_message(self, message, chat_id, sender_uuid):
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
            row = await conn.fetchrow("""
                INSERT INTO telegram_messages (
                    platform_message_id, chat_id, sender_id, text, caption,
                    media_type, platform_created_at, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (platform_message_id) DO NOTHING
                RETURNING id
            """,
            platform_msg_id, chat_uuid, sender_uuid, message.message, getattr(message, 'caption', None),
            media_type, message.date, json.dumps(message.to_dict(), default=_tg_json)
            )

            # Capture reaction counts at backfill time (item 1.11 — historical
            # messages already carry reactions in message.reactions).
            if row is not None:
                await self._capture_message_reaction_counts(conn, row["id"], message)
                # Capture poll state if this message is a poll (item 1.12).
                await self._capture_poll(conn, row["id"], message)

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
                _tg_json(counts),
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
                _tg_json(options),
                total_voters,
                _tg_json(vote_counts),
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
                }, worker=worker)
        except Exception as e:
            logger.debug("Profile photo download failed for %s: %s", chat_name, e)

    async def download_media(self, item: dict, worker: "TelegramWorker | None" = None):
        cid = item["content_id"]
        if self.is_known(cid):
            return

        filename = self.build_filename(
            item["entity_id"], item["entity_name"],
            item["content_type"], cid, extension=item.get("extension", "jpg")
        )

        # Use worker's per-account dir if provided, else fall back to legacy session dir.
        if worker is not None:
            base_dir = self._account_media_dir_for(worker)
        else:
            base_dir = self.account_media_dir
        dest_dir = base_dir / item["content_type"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename

        try:
            if "data" in item:
                data = item["data"]
            else:
                client = worker.client if worker else self._primary_client
                data = await client.download_media(item["media"], bytes)

            if not data:
                return

            sha = self.sha256_bytes(data)

            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)

            metadata = {
                "entity_id": item["entity_id"],
                "entity_name": item["entity_name"],
                "content_type": item["content_type"],
                "content_id": cid,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {})
            }
            self.save_json(metadata, dest_dir / f"{Path(filename).stem}_metadata.json")

            await self.insert_media_item(
                entity_id=item["entity_id"], entity_name=item["entity_name"],
                content_type=item["content_type"], content_id=cid,
                filename=filename, file_path=str(dest),
                file_size=len(data), sha256=sha, metadata=metadata
            )
            self._known_ids.add(cid)
        except Exception as e:
            if _is_flood_wait(e):
                raise
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))

    async def _handle_photo(self, worker: "TelegramWorker", message, chat_id: str, chat_name: str):
        await self.download_media({
            "entity_id": chat_id,
            "entity_name": chat_name,
            "content_type": "photo",
            "content_id": str(message.id),
            "media": message.media.photo,
            "raw": message.to_dict()
        }, worker=worker)

    async def _handle_document(self, worker: "TelegramWorker", message, chat_id: str, chat_name: str, mime: str) -> bool:
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
            "raw": message.to_dict()
        }, worker=worker)
        return True

    async def _scan_stories(self, worker: "TelegramWorker", targets: list[str]):
        try:
            from telethon.tl.functions.stories import GetPeerStoriesRequest
            client = worker.client
            for target in targets:
                if self._stop.is_set():
                    break
                try:
                    entity = await client.get_entity(int(target))
                except ValueError:
                    entity = await client.get_entity(target)

                chat_id = str(entity.id)
                chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or chat_id

                try:
                    result = await client(GetPeerStoriesRequest(peer=entity))
                    stories = getattr(result, "stories", None)
                    if not stories:
                        continue
                    story_items = getattr(stories, "stories", [])
                    for story in story_items:
                        if self._stop.is_set():
                            break
                        story_id = getattr(story, "id", None)
                        if not story_id:
                            continue
                        cid = f"story_{chat_id}_{story_id}"
                        if self.is_known(cid):
                            continue

                        media = getattr(story, "media", None)
                        if media:
                            is_video = hasattr(media, "video")
                            await self.download_media({
                                "entity_id": chat_id,
                                "entity_name": chat_name,
                                "content_type": "story_video" if is_video else "story",
                                "content_id": cid,
                                "media": media,
                                "extension": "mp4" if is_video else "jpg",
                                "raw": story.to_dict()
                            }, worker=worker)
                except Exception as e:
                    logger.debug("Story fetch failed for %s: %s", chat_name, e)
        except ImportError:
            pass

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

    async def collect_realtime(self):
        """Register Telethon event handlers on every connected worker and run forever.

        This is the @client.on(events.NewMessage) listener equivalent. New /
        edited / deleted messages and chat-action / user-update events are
        persisted to the unified telegram_* schema. Media is downloaded
        inline via download_message_media() rather than enqueued to Redis
        (the unified collector replaces the microservices' Redis queue).

        Runs until self._stop is set.
        """
        from telethon import events

        if not self._workers:
            self._workers = await self._spawn_workers()
        if not self._workers:
            logger.error("collect_realtime: no Telegram workers connected — bailing")
            return

        self._realtime_running = True
        for worker in self._workers:
            client = worker.client
            client.add_event_handler(
                lambda e, w=worker: self._on_new_message(w, e),
                events.NewMessage(),
            )
            client.add_event_handler(
                lambda e, w=worker: self._on_message_edited(w, e),
                events.MessageEdited(),
            )
            client.add_event_handler(
                lambda e, w=worker: self._on_message_deleted(w, e),
                events.MessageDeleted(),
            )
            client.add_event_handler(
                lambda e, w=worker: self._on_chat_action(w, e),
                events.ChatAction(),
            )
            client.add_event_handler(
                lambda e, w=worker: self._on_user_update(w, e),
                events.UserUpdate(),
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
                    events.Raw(types=[UpdateMessageReactions, UpdateBotMessageReactions]),
                )
            except Exception as exc:
                # Older Telethon may not expose UpdateBotMessageReactions; degrade.
                logger.warning(
                    "Reaction event registration failed (older Telethon?): %s",
                    exc,
                )
            logger.info(
                "[worker=%d account=%s] realtime handlers registered",
                worker.worker_id, worker.account.name,
            )

        logger.info(
            "Realtime listener running across %d worker(s); awaiting events…",
            len(self._workers),
        )
        # Park until stop. Telethon delivers events under each client's own task.
        while self._realtime_running and not self._stop.is_set():
            await asyncio.sleep(1.0)

    async def _on_new_message(self, worker: "TelegramWorker", event):
        try:
            chat_id = event.chat_id
            if self._hub_group_id is not None and chat_id == self._hub_group_id:
                return  # discard hub-group messages
            message = event.message
            await self._write_realtime_message(message, chat_id)
            sender = await event.get_sender()
            if sender is not None:
                await self._upsert_user_full(sender)
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
            await self._write_realtime_message(message, chat_id, is_edit=True)
        except Exception as exc:
            logger.error("_on_message_edited error: %s", exc, exc_info=True)

    async def _on_message_deleted(self, worker: "TelegramWorker", event):
        try:
            chat_id = event.chat_id
            for msg_id in (event.deleted_ids or []):
                async with self.pool.acquire() as conn:
                    # Mark the row deleted in metadata; ON CONFLICT NOTHING is
                    # fine because the row may not exist (deletion of a
                    # message we never saw).
                    await conn.execute("""
                        UPDATE telegram_messages
                        SET metadata = jsonb_set(
                                COALESCE(metadata, '{}'::jsonb),
                                '{deleted}', 'true'::jsonb, true
                            ),
                            updated_at = NOW()
                        WHERE platform_message_id = $1
                    """, f"{chat_id}:{msg_id}")
        except Exception as exc:
            logger.error("_on_message_deleted error: %s", exc, exc_info=True)

    async def _on_chat_action(self, worker: "TelegramWorker", event):
        """Translate Telethon chat actions into telegram_chat_members upserts."""
        try:
            chat_id = event.chat_id
            role = "member"
            if getattr(event, "user_kicked", False):
                role = "banned"
            elif getattr(event, "user_left", False):
                role = "left"
            user_ids: list[int] = []
            try:
                if getattr(event, "user_id", None):
                    user_ids.append(event.user_id)
            except Exception:
                pass
            if not user_ids:
                return
            async with self.pool.acquire() as conn:
                for user_id in user_ids:
                    await conn.execute("""
                        INSERT INTO telegram_chat_members
                            (chat_id, user_id, role, joined_at, last_seen_at, refreshed_at)
                        VALUES ($1, $2, $3, NOW(), NOW(), NOW())
                        ON CONFLICT (chat_id, user_id) DO UPDATE SET
                            role = EXCLUDED.role,
                            last_seen_at = NOW(),
                            refreshed_at = NOW()
                    """, int(chat_id), int(user_id), role)
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
                MessageReactions,
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
                    _tg_json(counts),
                    total,
                )
        except Exception as exc:
            logger.debug("_on_raw_reactions failed: %s", exc)

    async def _write_realtime_message(self, message, chat_id: int, is_edit: bool = False):
        """INSERT (or UPDATE-on-edit) the message into telegram_messages."""
        # Resolve UUIDs via the existing chat upsert chain. We don't have the
        # entity here so just key off platform_chat_id.
        async with self.pool.acquire() as conn:
            chat_row = await conn.fetchrow(
                "SELECT id FROM telegram_chats WHERE platform_chat_id = $1",
                str(chat_id),
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
            payload_json = (
                json.dumps(message.to_dict(), default=_tg_json)
                if hasattr(message, "to_dict") else "{}"
            )

            if is_edit:
                # Update existing if present; else insert.
                await conn.execute("""
                    INSERT INTO telegram_messages (
                        platform_message_id, chat_id, sender_id, text, caption,
                        media_type, platform_created_at, metadata
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (platform_message_id) DO UPDATE SET
                        text = EXCLUDED.text,
                        caption = EXCLUDED.caption,
                        metadata = EXCLUDED.metadata
                """,
                platform_msg_id, chat_uuid, sender_uuid,
                getattr(message, "message", None),
                getattr(message, "caption", None),
                media_type, message.date, payload_json,
                )
            else:
                await conn.execute("""
                    INSERT INTO telegram_messages (
                        platform_message_id, chat_id, sender_id, text, caption,
                        media_type, platform_created_at, metadata
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (platform_message_id) DO NOTHING
                """,
                platform_msg_id, chat_uuid, sender_uuid,
                getattr(message, "message", None),
                getattr(message, "caption", None),
                media_type, message.date, payload_json,
                )

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
                        platform_user_id, username, first_name, last_name, phone, bio, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, NOW())
                    ON CONFLICT (platform_user_id) DO UPDATE SET
                        username = COALESCE(EXCLUDED.username, telegram_users.username),
                        first_name = COALESCE(EXCLUDED.first_name, telegram_users.first_name),
                        last_name = COALESCE(EXCLUDED.last_name, telegram_users.last_name),
                        phone = COALESCE(EXCLUDED.phone, telegram_users.phone),
                        bio = COALESCE(EXCLUDED.bio, telegram_users.bio),
                        updated_at = NOW()
                """,
                str(user.id),
                new_row["username"],
                new_row["first_name"],
                new_row["last_name"],
                new_row["phone"],
                new_row["bio"],
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
                    await self._upsert_message(message, str(chat_id_int), sender_uuid)
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

        client = worker.client
        try:
            user = await client.get_entity(int(user_id))
        except (ValueError, TypeError):
            try:
                user = await client.get_entity(user_id)
            except Exception as exc:
                logger.warning("collect_user_profile resolve failed: %s", exc)
                return None
        except Exception as exc:
            logger.warning("collect_user_profile resolve failed: %s", exc)
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

        # Profile photo (first/largest).
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
                    }, worker=worker)
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
        try:
            entity = await client.get_entity(int(chat_id_str))
            chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or chat_id_str
        except Exception:
            pass

        from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
        if isinstance(message.media, MessageMediaPhoto):
            await self._handle_photo(worker, message, chat_id_str, chat_name)
            return True
        if isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            mime = getattr(doc, "mime_type", "") or ""
            await self._handle_document(worker, message, chat_id_str, chat_name, mime)
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
