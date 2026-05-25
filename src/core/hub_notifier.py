import asyncio
import logging
import os
import sqlite3
import time
from enum import Enum

logger = logging.getLogger(__name__)

_CACHE_DB_PATH = os.getenv("HUB_NOTIFY_CACHE_DB", "data/hub_cache.db")
_CACHE_BUSY_TIMEOUT_MS = 5000


class NotifyCategory(Enum):
    COLLECTION_START = "collection_start"
    COLLECTION_COMPLETE = "collection_complete"
    ERROR = "error"
    RATE_LIMIT = "rate_limit"
    DISCOVERY = "discovery"


class HubNotifier:

    def __init__(self, hub_group: str = "", min_interval: float = 60.0,
                 batch_interval: float = 30.0, enabled: bool = True):
        self._hub_group = hub_group or os.getenv("TELEGRAM_HUB_GROUP", "")
        self._min_interval = min_interval
        self._batch_interval = batch_interval
        self._enabled = enabled and bool(self._hub_group)
        self._last_sent: dict[str, float] = {}
        self._queue: dict[str, list[str]] = {c.value: [] for c in NotifyCategory}
        self._client = None
        self._flush_task: asyncio.Task | None = None
        self._supervisor_task: asyncio.Task | None = None
        self._running = False
        self._stats = {
            "messages_sent": 0,
            "messages_batched": 0,
            "messages_dropped": 0,
            "messages_cached": 0,
        }

    def set_client(self, client):
        self._client = client

    async def start(self):
        if not self._enabled:
            return
        if self._running:
            return
        self._running = True
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())

    async def stop(self):
        self._running = False

        if self._supervisor_task:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass

        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        try:
            await self._flush_all()
        except Exception:
            pass

        self._checkpoint_cache_db()

    async def _supervisor_loop(self):
        while self._running:
            try:
                if self._flush_task is None or self._flush_task.done():
                    if self._flush_task and self._flush_task.done():
                        try:
                            if exc := self._flush_task.exception():
                                logger.error("Flusher task died: %s", exc)
                        except (asyncio.CancelledError, asyncio.InvalidStateError):
                            pass
                    self._flush_task = asyncio.create_task(self._flush_loop())
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Supervisor loop error: %s", e)
                await asyncio.sleep(60)

    def notify(self, category: NotifyCategory, message: str, immediate: bool = False):
        if not self._enabled:
            return

        cat = category.value
        now = time.monotonic()

        if immediate:
            last = self._last_sent.get(cat, 0)
            if now - last >= self._min_interval:
                # Schedule on the running loop. If called from a non-loop
                # thread (sync code path), fall through to the queued path
                # rather than silently dropping the message.
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._send(f"[{cat.upper()}] {message}"))
                    self._last_sent[cat] = now
                    return
                except RuntimeError:
                    # No running loop — queue it so the flusher picks it up
                    # next tick. ERROR notifications will still bypass rate
                    # limit at flush time (see _flush_all).
                    pass

        self._queue[cat].append(message)
        self._stats["messages_batched"] += 1

    async def _flush_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self._batch_interval)
                await self._replay_cached()
                await self._flush_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Hub flush error: %s", e)
                await asyncio.sleep(5)

    async def _flush_all(self):
        now = time.monotonic()
        for cat, messages in self._queue.items():
            if not messages:
                continue

            # ERROR notifications bypass the per-category min_interval gate.
            # They're inherently rare-but-important; rate-limit applies
            # at the Telegram-API level, not here.
            is_error = (cat == NotifyCategory.ERROR.value)
            last = self._last_sent.get(cat, 0)
            if not is_error and now - last < self._min_interval:
                continue

            if len(messages) == 1:
                text = f"[{cat.upper()}] {messages[0]}"
            else:
                lines = "\n".join(f"  - {m}" for m in messages[-10:])
                text = f"[{cat.upper()}] {len(messages)} events:\n{lines}"

            sent = await self._send(text)
            if sent:
                self._last_sent[cat] = now
            messages.clear()

    async def _send(self, text: str) -> bool:
        if not self._client or not self._hub_group:
            logger.debug("Hub notify (no client): %s", text[:100])
            self._stats["messages_dropped"] += 1
            await self._cache_message(text)
            return False

        try:
            entity = self._hub_group
            if entity.startswith("@"):
                entity = entity[1:]
            try:
                await self._client.send_message(entity, text)
                self._stats["messages_sent"] += 1
                return True
            except Exception as e:
                if "input entity" in str(e).lower() or "peerchannel" in str(e).lower():
                    logger.warning("Entity error, re-resolving hub: %s", e)
                    try:
                        await self._client.get_entity(entity)
                        await self._client.send_message(entity, text)
                        self._stats["messages_sent"] += 1
                        return True
                    except Exception as retry_e:
                        logger.error("Re-resolve failed: %s", retry_e)
                raise
        except Exception as e:
            logger.debug("Hub send failed: %s", e)
            self._stats["messages_dropped"] += 1
            await self._cache_message(text)
            return False

    def _open_cache_conn(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(_CACHE_DB_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(_CACHE_DB_PATH, timeout=_CACHE_BUSY_TIMEOUT_MS / 1000)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=%d" % _CACHE_BUSY_TIMEOUT_MS)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pending_notifications "
            "(id INTEGER PRIMARY KEY, message TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        return conn

    async def _cache_message(self, message: str):
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._write_to_cache, message)
            self._stats["messages_cached"] += 1
        except Exception as e:
            logger.debug("Cache write failed: %s", e)

    def _write_to_cache(self, message: str, max_size: int = 500):
        conn = self._open_cache_conn()
        try:
            count = conn.execute("SELECT COUNT(*) FROM pending_notifications").fetchone()[0]
            if count >= max_size:
                conn.execute(
                    "DELETE FROM pending_notifications WHERE id IN "
                    "(SELECT id FROM pending_notifications ORDER BY created_at ASC LIMIT ?)",
                    (count - max_size + 1,),
                )
            conn.execute("INSERT INTO pending_notifications (message) VALUES (?)", (message,))
            conn.commit()
        finally:
            conn.close()

    async def _replay_cached(self):
        if not os.path.exists(_CACHE_DB_PATH):
            return

        try:
            loop = asyncio.get_running_loop()
            claimed = await loop.run_in_executor(None, self._claim_cache_items)
            if not claimed:
                return

            logger.info("Replaying %d cached notifications", len(claimed))
            to_requeue = []
            for i, (msg_id, message) in enumerate(claimed):
                sent = await self._send(f"[cached] {message}")
                if sent:
                    await asyncio.sleep(1)
                else:
                    to_requeue = claimed[i:]
                    break

            if to_requeue:
                await loop.run_in_executor(None, self._requeue_items, to_requeue)
        except Exception as e:
            logger.debug("Replay failed: %s", e)

    def _claim_cache_items(self, limit: int = 20) -> list:
        conn = self._open_cache_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, message FROM pending_notifications "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                conn.execute(
                    "DELETE FROM pending_notifications WHERE id IN (%s)"
                    % ",".join("?" * len(ids)),
                    ids,
                )
            conn.execute("COMMIT")
            return rows
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            return []
        finally:
            conn.close()

    def _requeue_items(self, items: list):
        conn = self._open_cache_conn()
        try:
            for _, message in items:
                conn.execute(
                    "INSERT INTO pending_notifications (message) VALUES (?)", (message,),
                )
            conn.commit()
        except Exception as e:
            logger.debug("Requeue failed: %s", e)
        finally:
            conn.close()

    def _checkpoint_cache_db(self):
        if not os.path.exists(_CACHE_DB_PATH):
            return
        try:
            conn = sqlite3.connect(_CACHE_DB_PATH, timeout=_CACHE_BUSY_TIMEOUT_MS / 1000)
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            conn.close()
        except Exception:
            pass

    def get_stats(self) -> dict:
        return dict(self._stats)
