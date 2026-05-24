"""
Hub Notifier - Centralized event notification system for Hub's General Topic.

Provides rate-limited, batched notifications for:
- Scanning progress (SCAN)
- Storing/database operations (STORE)
- Face detection and embedding (FACE)
- Errors and alerts (ERROR)

Uses batching to prevent flooding the Hub with individual messages.
"""
import atexit
import logging
import asyncio
import sqlite3
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional
from enum import Enum
from dataclasses import dataclass, field
from collections import defaultdict

_CACHE_DB_PATH = "hub_cache.db"
_CACHE_BUSY_TIMEOUT_MS = 5000

logger = logging.getLogger(__name__)


class EventCategory(Enum):
    """Categories of hub notification events."""
    SCAN = 'scan'       # Scanning progress
    STORE = 'store'     # Database/storage operations
    FACE = 'face'       # Face detection and embedding
    ERROR = 'error'     # Errors and alerts
    SYSTEM = 'system'   # System-level events


# Category emoji mapping
CATEGORY_EMOJI = {
    EventCategory.SCAN: '📂',
    EventCategory.STORE: '💾',
    EventCategory.FACE: '👥',
    EventCategory.ERROR: '⚠️',
    EventCategory.SYSTEM: '🔧'
}


@dataclass
class NotificationEvent:
    """A single notification event."""
    category: EventCategory
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    priority: int = 0  # Higher = more important, sent immediately


class HubNotifier:
    """
    Centralized notification system for sending updates to Hub's General Topic.
    
    Features:
    - Rate limiting to prevent Telegram flood
    - Batching of similar events
    - Priority-based immediate sending for critical events
    - Automatic background flushing
    """
    
    _instance: Optional['HubNotifier'] = None
    
    def __init__(
        self,
        batch_interval: int = 60,
        rate_limit_per_minute: int = 10,
        enable_notifications: bool = True
    ):
        """
        Args:
            batch_interval: Seconds between batched notification flushes
            rate_limit_per_minute: Maximum messages per minute
            enable_notifications: Master switch to enable/disable all notifications
        """
        self._event_queue: Dict[EventCategory, List[NotificationEvent]] = defaultdict(list)
        self._batch_interval = batch_interval
        self._rate_limit = rate_limit_per_minute
        self._messages_sent_this_minute = 0
        self._minute_start = datetime.now(timezone.utc)
        self._enabled = enable_notifications
        self._running = False
        self._flush_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        
        # Stats for batched summaries
        self._stats = {
            'faces_detected': 0,
            'identities_created': 0,
            'uploads_completed': 0,
            'messages_scanned': 0,
            'errors_count': 0
        }
        
        logger.info(f"HubNotifier initialized (batch_interval={batch_interval}s, rate_limit={rate_limit_per_minute}/min)")
        atexit.register(self._checkpoint_cache_db)
    
    @classmethod
    def reset_instance(cls):
        """Resets the singleton instance (e.g. on restart)."""
        if cls._instance:
            # Try to stop existing instance if running
            try:
                # Use get_event_loop as we might be in a crash state
                # but careful not to raise if no loop
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.get_event_loop()
                    
                if loop.is_running():
                    loop.create_task(cls._instance.stop())
            except Exception:
                pass
            cls._instance = None
            logger.info("HubNotifier instance reset")

    @classmethod
    def get_instance(cls) -> 'HubNotifier':
        """Gets or creates singleton instance."""
        if cls._instance is None:
            from shared.config import settings
            cls._instance = cls(
                batch_interval=getattr(settings, 'HUB_NOTIFY_BATCH_INTERVAL', 60),
                rate_limit_per_minute=getattr(settings, 'HUB_NOTIFY_RATE_LIMIT', 10),
                enable_notifications=True
            )
        return cls._instance
    
    async def start(self):
        """Starts the background flusher and its supervisor."""
        if self._running:
            return
        
        self._running = True
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())
        # Re-create lock on start to ensure it belongs to current loop
        self._lock = asyncio.Lock()
        logger.info("HubNotifier background flusher and supervisor started")
    
    async def _supervisor_loop(self):
        """Monitors the flusher task and restarts it if it dies."""
        while self._running:
            try:
                if self._flush_task is None or self._flush_task.done():
                    if self._flush_task and self._flush_task.done():
                        try:
                            # Check if it failed with an exception
                            if exc := self._flush_task.exception():
                                logger.error(f"Flusher task died unexpectedly: {exc}")
                        except (asyncio.CancelledError, asyncio.InvalidStateError):
                            pass
                        
                    logger.info("Starting/Restarting background flusher...")
                    self._flush_task = asyncio.create_task(self._background_flusher())
                
                await asyncio.sleep(10)  # Check every 10s
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Supervisor loop error: {e}")
                await asyncio.sleep(60)

    async def stop(self):
        """Stops the background flusher and flushes remaining events."""
        self._running = False
        
        if hasattr(self, '_supervisor_task') and self._supervisor_task:
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
        
        # Final flush - attempt only if loop is still running
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                await self.flush()
        except RuntimeError:
            logger.warning("Could not perform final flush: no running event loop")

        self._checkpoint_cache_db()
        logger.info("HubNotifier stopped")
    
    async def queue_event(
        self,
        category: str | EventCategory,
        message: str,
        priority: int = 0
    ):
        """
        Queues an event for notification.
        
        Args:
            category: Event category (SCAN, STORE, FACE, ERROR, SYSTEM)
            message: The notification message
            priority: 0=batched, 1=next flush, 2+=immediate
        """
        if not self._enabled:
            return
        
        # Convert string to enum if needed
        if isinstance(category, str):
            category = EventCategory(category.lower())
        
        event = NotificationEvent(
            category=category,
            message=message,
            priority=priority
        )
        
        # High priority events are sent immediately
        if priority >= 2:
            await self._send_immediate(event)
        else:
            async with self._lock:
                self._event_queue[category].append(event)
    
    async def increment_stat(self, stat_name: str, amount: int = 1):
        """Increments a stat counter for batched summaries (thread-safe)."""
        async with self._lock:
            if stat_name in self._stats:
                self._stats[stat_name] += amount
    
    async def _background_flusher(self):
        """Periodically flushes queued events."""
        logger.debug("HubNotifier background flusher loop started")
        
        while self._running:
            try:
                await asyncio.sleep(self._batch_interval)
                
                # First try to replay any cached notifications
                await self._replay_cached_notifications()
                
                # Then flush new ones
                await self.flush()
                
            except asyncio.CancelledError:
                logger.debug("HubNotifier flusher cancelled")
                break
            except Exception as e:
                import traceback
                logger.error(f"Background flusher error: {e}\n{traceback.format_exc()}")
                # Prevent tight loop if error persists
                await asyncio.sleep(5)
    
    async def flush(self):
        """Sends all queued events as a batched summary."""
        async with self._lock:
            if not self._event_queue and all(v == 0 for v in self._stats.values()):
                return  # Nothing to send
            
            # Build summary message
            lines = []
            now = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
            lines.append(f"📊 **Status Update** - `{now}`\n")
            
            # Add stats summary if any
            if any(v > 0 for v in self._stats.values()):
                lines.append("**Activity Summary:**")
                if self._stats['messages_scanned'] > 0:
                    lines.append(f"  • Messages scanned: `{self._stats['messages_scanned']}`")
                if self._stats['faces_detected'] > 0:
                    lines.append(f"  • Faces detected: `{self._stats['faces_detected']}`")
                if self._stats['identities_created'] > 0:
                    lines.append(f"  • New identities: `{self._stats['identities_created']}`")
                if self._stats['uploads_completed'] > 0:
                    lines.append(f"  • Uploads completed: `{self._stats['uploads_completed']}`")
                if self._stats['errors_count'] > 0:
                    lines.append(f"  • Errors: `{self._stats['errors_count']}`")
                lines.append("")
            
            # Add individual events by category
            for category in EventCategory:
                events = self._event_queue.get(category, [])
                if events:
                    emoji = CATEGORY_EMOJI.get(category, '📌')
                    lines.append(f"**{emoji} {category.value.upper()}:**")
                    
                    # Group similar messages
                    message_counts: Dict[str, int] = defaultdict(int)
                    for event in events:
                        message_counts[event.message] += 1
                    
                    for msg, count in list(message_counts.items())[:5]:  # Limit to 5 per category
                        if count > 1:
                            lines.append(f"  • {msg} (×{count})")
                        else:
                            lines.append(f"  • {msg}")
                    
                    if len(message_counts) > 5:
                        lines.append(f"  • _...and {len(message_counts) - 5} more_")
                    lines.append("")
            
            # Clear queue and reset stats
            self._event_queue.clear()
            for key in self._stats:
                self._stats[key] = 0
            
            if len(lines) > 2:  # More than just header
                message = '\n'.join(lines)
                await self._send_message(message)
    
    async def _send_immediate(self, event: NotificationEvent):
        """Sends a high-priority event immediately."""
        emoji = CATEGORY_EMOJI.get(event.category, '📌')
        message = f"{emoji} {event.message}"
        await self._send_message(message)
    
    async def _send_message(self, message: str):
        """Sends a message to the Hub with rate limiting."""
        # Check rate limit
        now = datetime.now(timezone.utc)
        if (now - self._minute_start).total_seconds() >= 60:
            self._minute_start = now
            self._messages_sent_this_minute = 0
        
        if self._messages_sent_this_minute >= self._rate_limit:
            logger.warning(f"Rate limit reached ({self._rate_limit}/min), caching message for later replay")
            await self._cache_notification(message)
            return False
        
        try:
            from services.collector.account_manager import bot_client_manager
            from shared.config import get_hub_group_id, resolve_hub_group_id, settings
            
            client = bot_client_manager.client
            hub_id = get_hub_group_id()
            if hub_id is None and client:
                try:
                    hub_id = await resolve_hub_group_id(client)
                except Exception:
                    hub_id = settings.HUB_GROUP_ID
            
            if client and hub_id:
                try:
                    await client.send_message(hub_id, message)
                    self._messages_sent_this_minute += 1
                    logger.debug(f"Sent notification to Hub ({self._messages_sent_this_minute}/{self._rate_limit})")
                    return True
                except Exception as e:
                    if "input entity" in str(e).lower() or "peerchannel" in str(e).lower():
                        logger.warning(f"Entity error sending to Hub, attempting to re-resolve: {e}")
                        try:
                            await client.get_entity(hub_id)
                            await client.send_message(hub_id, message)
                            self._messages_sent_this_minute += 1
                            logger.info("Successfully sent notification after re-resolving Hub entity")
                            return True
                        except Exception as retry_e:
                            logger.error(f"Failed again after retry: {retry_e}")
                    
                    logger.warning(f"Failed to send to Hub (network error): {e}")
                    raise e
        except Exception as e:
            logger.error(f"Failed to send Hub notification: {e}")
            # Cache locally if caching is enabled
            await self._cache_notification(message)
            return False

    async def _cache_notification(self, message: str):
        """Caches a notification locally when Hub is unreachable."""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._write_to_cache, _CACHE_DB_PATH, message)
            logger.info("Cached notification locally due to Hub failure")
        except Exception as e:
            logger.error(f"Failed to cache notification: {e}")

    def _open_cache_conn(self, db_path: str) -> sqlite3.Connection:
        """Opens a cache DB connection with WAL mode and busy timeout."""
        conn = sqlite3.connect(db_path, timeout=_CACHE_BUSY_TIMEOUT_MS / 1000)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=%d" % _CACHE_BUSY_TIMEOUT_MS)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pending_notifications "
            "(id INTEGER PRIMARY KEY, message TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        return conn

    def _write_to_cache(self, db_path: str, message: str, max_size: int = 500):
        """Synchronous write to SQLite cache with size limit."""
        conn = self._open_cache_conn(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM pending_notifications").fetchone()[0]
            if count >= max_size:
                conn.execute(
                    "DELETE FROM pending_notifications WHERE id IN "
                    "(SELECT id FROM pending_notifications ORDER BY created_at ASC LIMIT ?)",
                    (count - max_size + 1,)
                )
                logger.warning(f"Cache size limit ({max_size}) reached, dropped oldest entries")
            conn.execute("INSERT INTO pending_notifications (message) VALUES (?)", (message,))
            conn.commit()
        finally:
            conn.close()

    def _claim_cache_items(self, db_path: str, limit: int = 20) -> list:
        """Atomically claims (deletes) cache items via BEGIN IMMEDIATE.

        Using BEGIN IMMEDIATE prevents two processes from reading the same rows
        and sending duplicate Telegram messages.  Failed sends must be
        re-queued via _requeue_to_cache so nothing is lost.
        """
        conn = self._open_cache_conn(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, message FROM pending_notifications "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,)
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
        except Exception as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            logger.error(f"Failed to claim cache items: {e}")
            return []
        finally:
            conn.close()

    def _requeue_to_cache(self, db_path: str, messages: list) -> None:
        """Re-inserts failed messages so the next replay attempt retries them."""
        if not messages:
            return
        conn = self._open_cache_conn(db_path)
        try:
            for _, message in messages:
                conn.execute(
                    "INSERT INTO pending_notifications (message) VALUES (?)", (message,)
                )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to requeue messages to cache: {e}")
        finally:
            conn.close()

    def _checkpoint_cache_db(self) -> None:
        """WAL checkpoint + close on shutdown/atexit — safe to call from any thread."""
        if not os.path.exists(_CACHE_DB_PATH):
            return
        try:
            conn = sqlite3.connect(_CACHE_DB_PATH, timeout=_CACHE_BUSY_TIMEOUT_MS / 1000)
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            conn.close()
        except Exception as e:
            logger.debug(f"WAL checkpoint on shutdown (non-critical): {e}")

    async def _replay_cached_notifications(self):
        """Replays cached notifications when Hub is reachable.

        Claims items atomically (BEGIN IMMEDIATE delete) so two processes
        running simultaneously never send the same message twice.
        Failed sends are re-queued so nothing is dropped.
        """
        if not os.path.exists(_CACHE_DB_PATH):
            return

        try:
            loop = asyncio.get_running_loop()
            claimed = await loop.run_in_executor(
                None, self._claim_cache_items, _CACHE_DB_PATH
            )

            if not claimed:
                return

            logger.info(f"Replaying {len(claimed)} cached notifications")
            to_requeue = []

            for i, (msg_id, message) in enumerate(claimed):
                if await self._send_message(f"🔄 [Cached] {message}"):
                    await asyncio.sleep(1)
                else:
                    # Hub still unreachable — requeue this item and all remaining
                    to_requeue = claimed[i:]
                    break

            if to_requeue:
                await loop.run_in_executor(
                    None, self._requeue_to_cache, _CACHE_DB_PATH, to_requeue
                )

        except Exception as e:
            logger.error(f"Failed to replay cached notifications: {e}")


# Convenience function
async def notify(category: str, message: str, priority: int = 0):
    """
    Convenience function to queue a notification.
    
    Args:
        category: 'scan', 'store', 'face', 'error', 'system'
        message: The notification message
        priority: 0=batched, 1=next flush, 2+=immediate
    """
    notifier = HubNotifier.get_instance()
    await notifier.queue_event(category, message, priority)


async def increment_stat(stat_name: str, amount: int = 1):
    """Convenience function to increment a stat."""
    notifier = HubNotifier.get_instance()
    await notifier.increment_stat(stat_name, amount)
