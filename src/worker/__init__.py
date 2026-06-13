import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from src.collectors import get_collector, list_sources
from src.core.drive_check import check_drive, wait_for_drive
from src.db.connection import get_pool, close_pool

logger = logging.getLogger(__name__)


class WorkerService:
    """Runs collectors as supervised background tasks with watchdog restart."""

    # Realtime / push sources collect from a broker or live socket, NOT from a
    # DB target list. They MUST run even with zero collection_targets -- otherwise
    # the run loop's `if not targets: continue` skips collector.run() forever, the
    # broker consumer never starts, and pushed events are dropped (this was the
    # whatsapp empty-tables bug). They are also exempt from zero-progress
    # escalation: a realtime source with no messages arriving is legitimately
    # idle, not wedged.
    REALTIME_SOURCES = frozenset({"whatsapp", "beeper", "telegram"})

    def __init__(self):
        self.pool = None
        self._stop = asyncio.Event()
        self._tasks: dict[str, asyncio.Task] = {}
        self._crash_counts: dict[str, int] = {}
        self._started_at: float = 0
        self.watchdog_interval = 30
        self.max_restarts = 5
        self._dlq = None
        # AUTO-HEAL: per-source liveness heartbeat. _heartbeat[source] is the
        # monotonic time of the last observed progress (cycle start/finish). The
        # watchdog cancels+relaunches any task whose heartbeat is older than
        # hang_timeout even though the task is NOT done() -- this is the ONLY thing
        # that recovers a HUNG collector (frozen inside collector.run with no
        # exception). Crash-restart already handled the exception case; this closes
        # the silent-hang gap that froze tiktok/youtube/whatsapp.
        self._heartbeat: dict[str, float] = {}
        # A hung cycle is one that hasn't beat in this many seconds. Generous default
        # (collectors with big subprocess downloads can legitimately run minutes);
        # override per-deployment via COLLECTOR_HANG_TIMEOUT_SECONDS.
        self.hang_timeout = float(os.getenv("COLLECTOR_HANG_TIMEOUT_SECONDS", "1800"))
        # ZERO-PROGRESS AUTO-HEAL (the third failure mode). A cycle can finish
        # cleanly and quickly yet persist NOTHING because the wedge is in the
        # DB/session layer (write stall, SQLite lock, exhausted pool). Heartbeat
        # stays fresh (so hang-detection never fires) and no exception is raised
        # (so crash-restart never fires) -- the source loops finish->relaunch
        # forever producing zero output, invisible to both other paths. We track
        # a per-source streak of cycles that HAD targets but did not advance the
        # collector's media_items progress counter. At the soft limit we relaunch
        # with a FRESH collector (clears wedged pool/session handles); at the hard
        # limit we mark the source dead (alertable).
        self._collectors: dict[str, object] = {}
        self._progress_baseline: dict[str, int] = {}
        self._zero_progress_streak: dict[str, int] = {}
        self.zero_progress_limit = int(os.getenv("COLLECTOR_ZERO_PROGRESS_LIMIT", "5"))
        self.zero_progress_hard_limit = int(os.getenv("COLLECTOR_ZERO_PROGRESS_HARD_LIMIT", "12"))
        self._auth_paused: dict[str, bool] = {}
        self._auth_pause_since: dict[str, float] = {}
        self.auth_retry_interval = float(os.getenv("COLLECTOR_AUTH_RETRY_INTERVAL_SECONDS", "1800"))
        # Hang kills use a separate counter so repeated hang-and-recover cycles
        # do not consume the crash budget. Reset to 0 on a successful cycle.
        # Only permanently kills a source that hangs every single cycle with no
        # successful completion in between.
        self._hang_counts: dict[str, int] = {}
        self.max_hang_cycles = int(os.getenv("COLLECTOR_MAX_HANG_CYCLES", "10"))
        self._tg_bot_token = os.getenv("NOTIFY_TELEGRAM_BOT_TOKEN", "")
        self._tg_chat_id = os.getenv("NOTIFY_TELEGRAM_CHAT_ID", "")
        self._tg_thread_id = os.getenv("NOTIFY_TELEGRAM_THREAD_ID", "")
        self._last_net_notify: float = 0  # dedup network-down alerts (5 min cooldown)

    async def start(self, sources: list[str]):
        logger.info("Worker service starting with sources: %s", sources)
        self._started_at = time.monotonic()

        if not check_drive():
            logger.warning("Drive not available, waiting...")
            ok = await asyncio.get_event_loop().run_in_executor(
                None, wait_for_drive,
            )
            if not ok:
                logger.error("Drive never appeared, exiting")
                return

        # Wait for DB (may not be ready on cold boot).
        db_poll = 5.0
        while not self._stop.is_set():
            try:
                self.pool = await get_pool()
                await self._init_db()
                break
            except Exception as e:
                logger.warning("worker: DB not ready — retrying in %.0fs: %s", db_poll, e)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=db_poll)
                    return
                except asyncio.TimeoutError:
                    pass
                db_poll = min(db_poll * 1.5, 60.0)

        # File-based per-source config (Option A: files authoritative). Reads
        # config/sources/<source>.targets + <source>.env and syncs them into the DB /
        # process env BEFORE any collector launches, so an operator edits a text file
        # + `docker restart` and the new targets/tunables take effect. No-op for any
        # source without a config file. Never fatal -- a bad file logs + is skipped.
        try:
            from src.core.source_config import sync_source_configs
            await sync_source_configs(self.pool)
        except Exception:
            logger.warning("source_config sync failed (non-fatal)", exc_info=True)

        # Wait for internet before launching collectors. On cold boot or
        # network outage this blocks patiently instead of spamming errors.
        await self._wait_for_internet("startup")

        self._install_signal_handlers()

        await self._notify_telegram(
            f"🟢 <b>UnifiedCollector ONLINE</b>\n"
            f"Sources: {', '.join(sources)}"
        )

        for i, source in enumerate(sources):
            self._launch(source, startup_delay=i * 3.0)

        watchdog = asyncio.create_task(self._watchdog_loop())
        reporter = asyncio.create_task(self._health_reporter())

        # P3-4: start the DLQ consumer (previously dead code — dlq_consumer.py
        # existed but was never instantiated, so dead_letter_queue grew forever
        # with no retries). A generic handler re-enqueues the failed entity as a
        # pending collection_target so the normal worker loop re-attempts it.
        dlq_task = None
        if os.getenv("DLQ_CONSUMER_ENABLED", "true").lower() == "true":
            try:
                from src.core.dlq_consumer import DLQConsumer
                self._dlq = DLQConsumer(self.pool, max_retries=10, scan_interval_seconds=120.0)
                for source in sources:
                    self._dlq.register(source, self._make_dlq_handler(source))
                dlq_task = asyncio.create_task(self._dlq.run_forever())
                logger.info("DLQ consumer started for sources: %s", sources)
            except Exception:
                logger.warning("DLQ consumer failed to start", exc_info=True)
        else:
            logger.info("DLQ consumer disabled via DLQ_CONSUMER_ENABLED=false")

        await self._stop.wait()

        watchdog.cancel()
        reporter.cancel()
        if self._dlq is not None:
            self._dlq.stop()
        if dlq_task is not None:
            dlq_task.cancel()
        for t in self._tasks.values():
            t.cancel()
        all_tasks = [watchdog, reporter, *self._tasks.values()]
        if dlq_task is not None:
            all_tasks.append(dlq_task)
        await asyncio.gather(*all_tasks, return_exceptions=True)

        await self._report_health("stopped")
        await close_pool()
        logger.info("Worker service stopped")

    async def _init_db(self):
        # P0-1/P0-2: ledger-backed runner applies schemas/ + migrations/.
        from src.db.migrate import apply_all
        await apply_all(self.pool)

    _NETWORK_ERRORS = (
        ConnectionError, ConnectionRefusedError, ConnectionResetError,
        OSError, TimeoutError, asyncio.TimeoutError,
    )

    @staticmethod
    def _is_network_error(exc: Exception) -> bool:
        """Return True if *exc* looks like an internet-connectivity failure."""
        if isinstance(exc, WorkerService._NETWORK_ERRORS):
            return True
        name = type(exc).__name__
        if name in ("ConnectError", "ReadTimeout", "PoolTimeout",
                     "NetworkError", "ClientConnectorError"):
            return True
        msg = str(exc).lower()
        return any(kw in msg for kw in (
            "connect", "unreachable", "name resolution",
            "no route", "network is down", "temporary failure",
            "getaddrinfo", "errno 11001", "dns",
        ))

    async def _check_internet(self) -> bool:
        """Quick non-blocking internet probe via a thread."""
        import urllib.request, concurrent.futures
        def _probe():
            try:
                r = urllib.request.urlopen(
                    "http://connectivitycheck.gstatic.com/generate_204", timeout=15
                )
                return r.status == 204 or r.status == 200
            except Exception:
                return False
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(pool, _probe), timeout=20.0
                )
            except (asyncio.TimeoutError, Exception):
                return False

    async def _wait_for_internet(self, context: str = "startup"):
        """Block until internet is reachable, polling with backoff. Logs once on loss, once on recovery."""
        if await self._check_internet():
            logger.info("worker: internet available (%s)", context)
            return
        logger.warning("worker: no internet (%s) — waiting patiently...", context)
        poll = 10.0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll)
                return
            except asyncio.TimeoutError:
                pass
            if await self._check_internet():
                logger.info("worker: internet restored (%s)", context)
                return
            poll = min(poll * 1.5, 120.0)

    _AUTH_ERROR_CLASS_NAMES = frozenset({
        "AuthKeyError", "AuthKeyUnregisteredError", "SessionRevokedError",
        "UserDeactivatedError", "UserDeactivatedBanError",
        "TwoFactorAuthRequiredException", "LoginRequiredException",
        "RefreshError",
    })

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        """Return True if *exc* looks like an authentication/session failure."""
        cls_name = type(exc).__name__
        if cls_name in WorkerService._AUTH_ERROR_CLASS_NAMES:
            return True
        for klass in type(exc).__mro__:
            if klass.__name__ in WorkerService._AUTH_ERROR_CLASS_NAMES:
                return True
        if cls_name == "BeeperAPIError":
            msg = str(exc).lower()
            if any(kw in msg for kw in ("401", "403", "unauthorized", "token")):
                return True
        if cls_name == "HTTPStatusError":
            response = getattr(exc, "response", None)
            if response is not None and getattr(response, "status_code", 0) == 401:
                return True
        if isinstance(exc, RuntimeError):
            msg = str(exc).lower()
            if "not authorized" in msg and "session" in msg:
                return True
        msg = str(exc).lower()
        auth_keywords = (
            "login_required", "challenge_required", "session revoked",
            "auth key unregistered", "user deactivated",
            "invalid_grant", "token has been expired or revoked",
            "session not authorized",
        )
        return any(kw in msg for kw in auth_keywords)

    async def _notify_telegram(self, message: str):
        """Best-effort Telegram bot notification."""
        if not self._tg_bot_token or not self._tg_chat_id:
            return
        import urllib.request, json as _json, concurrent.futures
        def _send():
            try:
                params = {
                    "chat_id": self._tg_chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                }
                if self._tg_thread_id:
                    params["message_thread_id"] = self._tg_thread_id
                data = _json.dumps(params).encode("utf-8")
                url = f"https://api.telegram.org/bot{self._tg_bot_token}/sendMessage"
                req = urllib.request.Request(url, data, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=15)
            except Exception:
                pass
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            try:
                await asyncio.wait_for(loop.run_in_executor(pool, _send), timeout=20.0)
            except Exception:
                pass

    async def _mark_source_auth_paused(self, source: str, error: str):
        self._auth_paused[source] = True
        self._auth_pause_since.setdefault(source, time.monotonic())
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO source_health "
                    "  (source, status, last_error, crash_count, updated_at) "
                    "VALUES ($1, 'auth_paused', $2, $3, NOW()) "
                    "ON CONFLICT (source) DO UPDATE "
                    "SET status='auth_paused', last_error=$2, updated_at=NOW()",
                    source, error[:2000], self._crash_counts.get(source, 0),
                )
        except Exception:
            logger.warning("Failed to persist auth-pause for %s", source, exc_info=True)
        await self._notify_telegram(
            f"🔑 <b>AUTH FAILURE: {source}</b>\n"
            f"Credentials/session broken — source paused.\n"
            f"Retrying every {int(self.auth_retry_interval)}s.\n"
            f"<code>{error[:300]}</code>"
        )

    async def _clear_auth_pause(self, source: str):
        if not self._auth_paused.get(source):
            return
        self._auth_paused[source] = False
        self._auth_pause_since.pop(source, None)
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE source_health SET status='running', "
                    "  last_error=NULL, updated_at=NOW() "
                    "WHERE source=$1 AND status='auth_paused'",
                    source,
                )
            logger.info("AUTH RECOVERED: %s credentials working again", source)
        except Exception:
            logger.warning("Failed to clear auth-pause for %s", source, exc_info=True)
        await self._notify_telegram(
            f"✅ <b>AUTH RECOVERED: {source}</b>\n"
            f"Credentials working again — collection resumed."
        )

    def _install_signal_handlers(self):
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_signal)
            except NotImplementedError:
                signal.signal(sig, lambda *_: self._handle_signal())

    def _make_dlq_handler(self, source: str):
        """P3-4: build a generic DLQ retry handler for a source.

        The handler re-enqueues the failed entity as a pending collection_target
        so the normal worker loop re-attempts it on its next pass. Raising
        propagates to the consumer's retry/backoff logic.
        """
        async def _handler(row: dict) -> None:
            entity = row.get("entity_id") or row.get("content_id")
            if not entity:
                from src.core.dlq_consumer import PermanentError
                raise PermanentError("DLQ row has no entity_id/content_id")
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO collection_targets (source, target_id, status, priority) "
                    "VALUES ($1, $2, 'pending', 1) "
                    "ON CONFLICT (source, target_id) DO UPDATE "
                    "SET status='pending' "
                    "WHERE collection_targets.status IN ('error', 'completed', 'active')",
                    source, str(entity),
                )
        return _handler

    def _handle_signal(self):
        logger.info("Shutdown signal received")
        self._stop.set()

    def _launch(self, source: str, startup_delay: float = 0.0):
        if source in self._tasks and not self._tasks[source].done():
            return
        self._crash_counts.setdefault(source, 0)
        task = asyncio.create_task(self._run_source(source, startup_delay=startup_delay), name=f"worker-{source}")
        self._tasks[source] = task
        self._ever_launched = True
        logger.info("Launched worker for %s", source)

    async def _run_source(self, source: str, startup_delay: float = 0.0):
        if startup_delay > 0:
            logger.info("worker/%s: staggered startup, waiting %.0fs", source, startup_delay)
            await asyncio.sleep(startup_delay)
        # Fresh collector on every (re)launch. This is what makes the zero-progress
        # SOFT escalation effective: cancelling the task and relaunching builds a
        # brand-new collector instance here, dropping any wedged asyncpg pool /
        # Telethon session / broker handle the old instance was stuck on.
        collector = get_collector(source)
        collector.set_pool(self.pool)
        self._collectors[source] = collector
        # Reset the progress baseline to THIS collector's counter (a fresh
        # instance starts at 0) so a relaunch doesn't inherit a stale streak.
        self._progress_baseline[source] = collector.progress_count
        self._zero_progress_streak[source] = 0

        while not self._stop.is_set():
            try:
                self._heartbeat[source] = time.monotonic()
                targets = await self._load_targets(source)
                is_realtime = source in self.REALTIME_SOURCES
                if not targets and not is_realtime:
                    logger.debug("No targets for %s, sleeping 60s", source)
                    # No-targets is NOT a zero-progress wedge -- it's legitimate
                    # idle (e.g. all targets marked completed / dedup-exhausted).
                    # Reset the streak so we never escalate a healthy idle source.
                    self._zero_progress_streak[source] = 0
                    self._progress_baseline[source] = collector.progress_count
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=60)
                    except asyncio.TimeoutError:
                        pass
                    continue

                # Realtime/push sources run even with zero targets: collector.run([])
                # starts the broker consumer / live listener which blocks until the
                # source is stopped. They collect from ALL connected accounts'
                # chats/groups/channels (no DB target list needed).
                if is_realtime and not targets:
                    logger.info("Running realtime source %s (no target list; "
                                "collecting from all connected accounts)", source)
                else:
                    logger.info("Running %s with %d targets", source, len(targets))
                self._heartbeat[source] = time.monotonic()
                before = collector.progress_count
                await collector.run(targets)
                self._heartbeat[source] = time.monotonic()
                self._crash_counts[source] = 0
                self._hang_counts[source] = 0  # successful cycle clears hang budget
                if self._auth_paused.get(source):
                    await self._clear_auth_pause(source)

                # ZERO-PROGRESS accounting: had targets, cycle finished. Did it
                # actually persist anything new? Realtime sources are EXEMPT --
                # a quiet broker (no messages arriving) is legitimate idle, and
                # collector.run() blocks for them anyway so this rarely runs.
                advanced = collector.progress_count - before
                if is_realtime or advanced > 0:
                    self._zero_progress_streak[source] = 0
                else:
                    self._zero_progress_streak[source] = (
                        self._zero_progress_streak.get(source, 0) + 1
                    )
                    streak = self._zero_progress_streak[source]
                    logger.warning(
                        "worker/%s: zero-progress cycle %d/%d (had %d targets, "
                        "persisted 0)", source, streak, self.zero_progress_limit,
                        len(targets),
                    )

                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=300)
                except asyncio.TimeoutError:
                    pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._is_network_error(e):
                    logger.warning("%s: network error — waiting for internet: %s",
                                   source, e)
                    now = time.monotonic()
                    if now - self._last_net_notify > 300:
                        self._last_net_notify = now
                        await self._notify_telegram(
                            f"🌐 <b>NETWORK DOWN</b>\n"
                            f"Internet lost ({source}). Waiting to recover.\n"
                            f"<code>{str(e)[:200]}</code>"
                        )
                    await self._wait_for_internet(f"{source}/recovery")
                    if now == self._last_net_notify:
                        await self._notify_telegram("✅ <b>NETWORK RESTORED</b>\nInternet recovered — all collectors resuming.")
                    continue
                if self._is_auth_error(e):
                    logger.warning(
                        "AUTH FAILURE: %s — credentials/session broken. "
                        "Paused (retrying every %.0fs). Error: %s",
                        source, self.auth_retry_interval, e,
                    )
                    await self._mark_source_auth_paused(source, repr(e))
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=self.auth_retry_interval
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue
                self._crash_counts[source] += 1
                count = self._crash_counts[source]
                logger.error("%s crashed (%d/%d): %r", source, count, self.max_restarts, e,
                             exc_info=True)
                if count >= self.max_restarts:
                    logger.error("%s exceeded max restarts, giving up", source)
                    await self._mark_source_dead(source, repr(e), count)
                    await self._notify_telegram(
                        f"💀 <b>SOURCE DEAD: {source}</b>\n"
                        f"Crashed {count}x, permanently stopped.\n"
                        f"<code>{repr(e)[:300]}</code>\n"
                        f"Fix: docker compose restart collector_{source}"
                    )
                    break
                backoff = min(300, 30 * (2 ** (count - 1)))
                logger.info("Restarting %s in %ds", source, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass

    async def _load_targets(self, source: str) -> list[str]:
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT target_id FROM collection_targets "
                    "WHERE source = $1 AND status IN ('pending', 'error') "
                    "ORDER BY priority DESC",
                    source,
                )
            return [r["target_id"] for r in rows]
        except Exception:
            # Don't pretend "no targets" — that hides real DB outages.
            # Log with stack and return empty so the run loop sleeps and
            # retries, but operators can see the failure.
            logger.exception("Failed to load targets for source=%s", source)
            return []

    async def _self_heal_exit(self, reason: str):
        """Last-resort self-healing: exit the process so Docker's restart policy
        (``restart: unless-stopped``) brings the container back with a clean slate.

        This is the recovery path for in-process wedges that a soft collector
        relaunch CANNOT clear because the corruption lives below the collector
        object: a desynced Telethon MTProto session, a dead asyncpg pool the loop
        can't rebuild, an OS-level SQLite lock. The watchdog already detects these
        (zero-progress HARD tier, max hang cycles, all-sources-dead) but previously
        only marked the source dead and gave up. Exiting hands recovery to Docker.

        Gated by COLLECTOR_SELF_HEAL_RESTART (default "true"). Abrupt by design
        (os._exit): a graceful shutdown could hang on the very wedge we're escaping.
        """
        if os.getenv("COLLECTOR_SELF_HEAL_RESTART", "true").lower() != "true":
            logger.error(
                "Self-heal restart DISABLED (COLLECTOR_SELF_HEAL_RESTART=false) — "
                "NOT restarting despite terminal wedge: %s", reason,
            )
            return
        logger.error("SELF-HEAL: exiting for clean container restart — %s", reason)
        try:
            await self._notify_telegram(
                f"🔄 <b>SELF-HEAL RESTART</b>\n"
                f"<code>{reason[:300]}</code>\n"
                f"Container exiting; Docker will restart it with a clean slate."
            )
        except Exception:
            pass
        import sys as _sys
        try:
            _sys.stdout.flush()
            _sys.stderr.flush()
        except Exception:
            pass
        # Hard-exit so the restart policy relaunches us. 42 = self-heal sentinel.
        os._exit(42)

    async def _watchdog_loop(self):
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.watchdog_interval)
                break
            except asyncio.TimeoutError:
                pass

            for source, task in list(self._tasks.items()):
                if task.done():
                    crashes = self._crash_counts.get(source, 0)
                    if crashes >= self.max_restarts:
                        # Permanently dead — remove from tracking to stop log spam.
                        logger.debug("Watchdog: %s is dead (crashes=%d), removing from watch", source, crashes)
                        del self._tasks[source]
                        continue
                    exc = task.exception() if not task.cancelled() else None
                    if exc:
                        logger.warning("Watchdog: %s died (%s), relaunching", source, exc)
                    else:
                        logger.info("Watchdog: %s finished, relaunching", source)
                    self._launch(source)
                    continue

                # AUTO-HEAL hung tasks: a task that is NOT done but hasn't beat its
                # heartbeat within hang_timeout is wedged (frozen inside
                # collector.run with no exception). Cancel + relaunch it -- this is
                # the recovery path that crash-restart can't reach. Without this a
                # silent hang (lost SIGCHLD, stuck socket, deadlocked broker wait)
                # leaves the source dead forever with zero auto-recovery.
                last = self._heartbeat.get(source)
                if last is None:
                    continue
                stalled = time.monotonic() - last
                if stalled > self.hang_timeout:
                    # Hangs use a separate budget from crash exceptions so that
                    # a source that hangs occasionally but also completes cycles
                    # successfully is never permanently killed. _hang_counts resets
                    # to 0 on every successful cycle.
                    hangs = self._hang_counts.get(source, 0) + 1
                    self._hang_counts[source] = hangs
                    logger.error(
                        "Watchdog: %s HUNG (no progress for %.0fs > %.0fs limit) "
                        "(%d/%d hangs) -- cancelling; relaunch on next pass",
                        source, stalled, self.hang_timeout, hangs, self.max_hang_cycles,
                    )
                    task.cancel()
                    self._heartbeat[source] = time.monotonic()
                    if hangs >= self.max_hang_cycles:
                        logger.error("Watchdog: %s exceeded max hang cycles (%d), giving up", source, hangs)
                        await self._mark_source_dead(source, f"hung > {self.hang_timeout:.0f}s (x{hangs})", hangs)
                        self._crash_counts[source] = self.max_restarts
                        # Self-heal: a repeatedly-hung source means a stuck socket /
                        # deadlocked broker wait that cancel+relaunch can't free.
                        await self._self_heal_exit(
                            f"{source}: hung x{hangs} (cancel+relaunch did not free it)"
                        )
                    continue

                # ZERO-PROGRESS escalation: the task is alive (not done), beating
                # its heartbeat (not hung), but has finished N consecutive cycles
                # that had targets yet persisted nothing. The wedge is in the
                # DB/session layer, invisible to crash- and hang-detection.
                streak = self._zero_progress_streak.get(source, 0)
                if streak >= self.zero_progress_hard_limit:
                    # HARD tier: relaunching with a fresh collector didn't help
                    # either -- the wedge is below the process (host SQLite lock,
                    # dead pool the loop can't rebuild). Mark dead so it's
                    # alertable; a container restart (Docker restart policy) is
                    # the real recovery for an OS-level lock.
                    logger.error(
                        "Watchdog: %s ZERO-PROGRESS HARD (%d cycles, soft relaunch "
                        "did not help) -- marking dead", source, streak,
                    )
                    await self._mark_source_dead(
                        source, f"zero-progress x{streak} (hard)", streak,
                    )
                    self._crash_counts[source] = self.max_restarts
                    task.cancel()
                    self._zero_progress_streak[source] = 0
                    self._heartbeat[source] = time.monotonic()
                    # Self-heal: soft relaunch failed, so the wedge is in-process
                    # (e.g. desynced Telethon session). A clean container restart
                    # is the documented recovery — actually do it now.
                    await self._self_heal_exit(
                        f"{source}: zero-progress hard x{streak} (in-process wedge)"
                    )
                elif streak >= self.zero_progress_limit:
                    # SOFT tier: cancel + relaunch. The relaunch (via _run_source)
                    # builds a FRESH collector, dropping the wedged pool/session
                    # handle. Don't relaunch inline -- cancel now and let the
                    # done-branch relaunch on the next pass (same pattern as hang).
                    logger.error(
                        "Watchdog: %s ZERO-PROGRESS SOFT (%d/%d cycles, no items "
                        "persisted) -- cancelling for fresh-collector relaunch",
                        source, streak, self.zero_progress_limit,
                    )
                    self._zero_progress_streak[source] = 0
                    task.cancel()
                    self._heartbeat[source] = time.monotonic()

            # All launched sources have died and been removed from tracking → the
            # container is alive but doing nothing. Self-heal: restart so every
            # source comes back fresh (the done-branch only relaunches sources
            # still under max_restarts; permanently-dead ones are dropped here).
            if getattr(self, "_ever_launched", False) and not self._tasks:
                await self._self_heal_exit("all sources dead — no live collectors remain")

    async def _health_reporter(self):
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
                break
            except asyncio.TimeoutError:
                pass
            await self._report_health("running")

    async def _report_health(self, status: str):
        try:
            active = sum(1 for t in self._tasks.values() if not t.done())
            total_crashes = sum(self._crash_counts.values())
            uptime = int(time.monotonic() - self._started_at)
            payload = json.dumps({
                "status": status,
                "active_workers": active,
                "total_workers": len(self._tasks),
                "crash_count": total_crashes,
                "uptime_seconds": uptime,
                "drive_ok": check_drive(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO service_cursors (service, last_processed_at, status) "
                    "VALUES ('_worker', NOW(), $1) "
                    "ON CONFLICT (service) DO UPDATE "
                    "SET last_processed_at = NOW(), status = $1",
                    status,
                )
        except Exception:
            # Operators won't see worker liveness if this fails silently.
            # Use warning + stack so degraded health is observable.
            logger.warning("Health report failed", exc_info=True)

    async def _mark_source_dead(self, source: str, error: str, crash_count: int):
        """P2-4: persist a permanently-dead source so it's queryable + alertable.

        The /metrics endpoint surfaces dead sources via uc_source_dead; an
        operator alert can fire on status='dead' instead of relying on someone
        noticing a single 'giving up' log line.
        """
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO source_health "
                    "  (source, status, last_error, crash_count, died_at, updated_at) "
                    "VALUES ($1, 'dead', $2, $3, NOW(), NOW()) "
                    "ON CONFLICT (source) DO UPDATE "
                    "SET status='dead', last_error=$2, crash_count=$3, "
                    "    died_at=NOW(), updated_at=NOW()",
                    source, error[:2000], crash_count,
                )
            logger.critical("SOURCE DEAD: %s gave up after %d crashes — %s",
                            source, crash_count, error[:200])
        except Exception:
            logger.warning("Failed to persist source death for %s", source, exc_info=True)

    def get_health(self) -> dict:
        active = sum(1 for t in self._tasks.values() if not t.done())
        return {
            "running": not self._stop.is_set(),
            "active_workers": active,
            "total_workers": len(self._tasks),
            "crash_count": sum(self._crash_counts.values()),
            "uptime_seconds": int(time.monotonic() - self._started_at) if self._started_at else 0,
            "sources": {
                s: {
                    "alive": not t.done(),
                    "crashes": self._crash_counts.get(s, 0),
                    "auth_paused": self._auth_paused.get(s, False),
                    "auth_paused_seconds": (
                        int(time.monotonic() - self._auth_pause_since[s])
                        if s in self._auth_pause_since and self._auth_paused.get(s)
                        else None
                    ),
                }
                for s, t in self._tasks.items()
            },
        }


_instance: WorkerService | None = None


async def run_worker(sources: list[str]):
    global _instance
    _instance = WorkerService()
    await _instance.start(sources)


def get_worker_health() -> dict:
    if _instance is None:
        return {"running": False, "active_workers": 0, "total_workers": 0, "crash_count": 0}
    return _instance.get_health()
