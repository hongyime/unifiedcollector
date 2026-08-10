"""Dead-letter-queue consumer (Wave 2.5).

Reads ``dead_letter_queue`` rows whose ``next_retry_at`` is due and
asks a per-source handler to retry. On success: deletes the row. On
failure: bumps ``retry_count`` and schedules ``next_retry_at`` with
exponential backoff. After ``max_retries`` the row is marked
``status='failed'`` and never claimed again.

Concurrency model
-----------------
Multiple consumers can run safely. The claim query uses
``SELECT ... FOR UPDATE SKIP LOCKED`` so two consumers asking for
work at the same time get DISJOINT rows; no double-handling.

Inside the claim transaction we also flip ``status`` to
``'in_progress'`` — that way an interrupted consumer's rows will
NOT be claimed by another consumer until the orphan recovery sweep
(see ``recover_orphans``) runs. Callers should periodically call
``recover_orphans(stale_after=300)`` to flip in_progress rows
older than 5 minutes back to pending.

Backoff
-------

    delay = base_seconds * (2 ** retry_count) + jitter
    delay = min(delay, max_backoff_seconds)
    jitter = random.uniform(0, base_seconds)

Defaults: base=30s, max=3600s (1h). After ~7 retries you've hit
the cap. After ``max_retries=10`` the row is marked failed.

Handler contract
----------------

    async def handler(row: dict) -> None:
        # row keys: id, source, entity_id, content_id, error_message,
        #           retry_count, created_at, next_retry_at,
        #           last_attempt_at, status
        # raise on retryable failure -> consumer schedules another attempt
        # raise PermanentError -> consumer marks status='failed' immediately
        # return normally -> consumer deletes the row

Register handlers per source via ``DLQConsumer.register(source, fn)``.
A row whose source has no handler is left alone (logged once per scan).
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any, Awaitable, Callable, Optional

import asyncpg

logger = logging.getLogger(__name__)

# Type aliases
HandlerFn = Callable[[dict], Awaitable[None]]


class PermanentError(Exception):
    """Raise from a handler to mark the row failed immediately."""


def compute_next_retry(
    retry_count: int,
    *,
    base_seconds: float = 30.0,
    max_backoff_seconds: float = 3600.0,
) -> float:
    """Exponential backoff + jitter. Returns seconds until next attempt."""
    if retry_count < 0:
        retry_count = 0
    delay = base_seconds * (2 ** retry_count)
    delay += random.uniform(0, base_seconds)
    return min(delay, max_backoff_seconds)


class DLQConsumer:
    """Async consumer for the dead_letter_queue table."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        max_retries: int = 10,
        base_backoff_seconds: float = 30.0,
        max_backoff_seconds: float = 3600.0,
        batch_size: int = 16,
        scan_interval_seconds: float = 60.0,
        db_timeout_seconds: float | None = None,
        handler_timeout_seconds: float | None = None,
    ):
        if pool is None:
            raise ValueError("pool must not be None")
        if max_retries < 1:
            raise ValueError("max_retries must be >=1")
        if batch_size < 1:
            raise ValueError("batch_size must be >=1")
        if scan_interval_seconds <= 0:
            raise ValueError("scan_interval_seconds must be >0")
        if db_timeout_seconds is None:
            raw_timeout = os.getenv("DLQ_CONSUMER_DB_TIMEOUT_SECONDS", "20")
            try:
                db_timeout_seconds = float(raw_timeout)
            except ValueError:
                db_timeout_seconds = 20.0
        if db_timeout_seconds <= 0:
            raise ValueError("db_timeout_seconds must be >0")
        if handler_timeout_seconds is None:
            raw_handler_timeout = os.getenv("DLQ_CONSUMER_HANDLER_TIMEOUT_SECONDS", "180")
            try:
                handler_timeout_seconds = float(raw_handler_timeout)
            except ValueError:
                handler_timeout_seconds = 180.0
        if handler_timeout_seconds <= 0:
            raise ValueError("handler_timeout_seconds must be >0")

        self._pool = pool
        self.max_retries = max_retries
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.batch_size = batch_size
        self.scan_interval_seconds = scan_interval_seconds
        self.db_timeout_seconds = db_timeout_seconds
        self.handler_timeout_seconds = handler_timeout_seconds

        self._handlers: dict[str, HandlerFn] = {}
        self._stop = asyncio.Event()

        # stats (best-effort; not under a lock — read-while-write is OK
        # because callers care about magnitude not exact counts)
        self.stats = {
            "scans": 0,
            "claimed": 0,
            "succeeded": 0,
            "retried": 0,
            "failed": 0,
            "orphans_recovered": 0,
        }

    # -- handler registration ------------------------------------------

    def register(self, source: str, fn: HandlerFn) -> None:
        """Register an async handler for rows with ``source``."""
        if not source:
            raise ValueError("source must not be empty")
        if not callable(fn):
            raise ValueError("fn must be callable")
        self._handlers[source] = fn

    def unregister(self, source: str) -> None:
        self._handlers.pop(source, None)

    # -- claim + execute --------------------------------------------------

    async def _claim_batch(self) -> list[dict]:
        """Atomically claim up to ``batch_size`` due rows.

        Uses SELECT ... FOR UPDATE SKIP LOCKED so concurrent consumers
        don't double-claim. Flips status to 'in_progress' under the
        same lock.

        Only claims rows whose ``source`` has a registered handler —
        prevents an idle consumer from grabbing rows that another
        process owns, and means tests with a unique source string
        won't see unrelated rows.
        """
        registered = list(self._handlers.keys())
        if not registered:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH claimed AS (
                    SELECT id
                    FROM dead_letter_queue
                    WHERE status = 'pending'
                      AND next_retry_at <= NOW()
                      AND source = ANY($2::text[])
                    ORDER BY next_retry_at
                    LIMIT $1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE dead_letter_queue AS dlq
                SET status = 'in_progress',
                    last_attempt_at = NOW()
                FROM claimed
                WHERE dlq.id = claimed.id
                RETURNING dlq.id, dlq.source, dlq.entity_id, dlq.content_id,
                          dlq.error_message, dlq.retry_count, dlq.created_at,
                          dlq.next_retry_at, dlq.last_attempt_at, dlq.status
                """,
                self.batch_size,
                registered,
                timeout=self.db_timeout_seconds,
            )
            return [dict(r) for r in rows]

    async def _on_success(self, row_id: int) -> None:
        """Delete the row from the DLQ."""
        await self._pool.execute(
            "DELETE FROM dead_letter_queue WHERE id=$1", row_id
        )

    async def _on_retry(
        self, row: dict, error_message: str,
    ) -> None:
        """Bump retry_count + schedule next attempt OR mark failed."""
        new_count = row["retry_count"] + 1
        if new_count >= self.max_retries:
            await self._pool.execute(
                """
                UPDATE dead_letter_queue
                SET status = 'failed',
                    retry_count = $2,
                    error_message = $3,
                    last_attempt_at = NOW()
                WHERE id = $1
                """,
                row["id"], new_count, error_message[:8000],
            )
            self.stats["failed"] += 1
            logger.warning(
                "DLQ id=%s source=%s entity=%s exhausted retries (%d) -> failed",
                row["id"], row["source"], row["entity_id"], new_count,
            )
            return

        delay = compute_next_retry(
            new_count,
            base_seconds=self.base_backoff_seconds,
            max_backoff_seconds=self.max_backoff_seconds,
        )
        await self._pool.execute(
            f"""
            UPDATE dead_letter_queue
            SET status = 'pending',
                retry_count = $2,
                error_message = $3,
                last_attempt_at = NOW(),
                next_retry_at = NOW() + INTERVAL '1 second' * {delay:.6f}
            WHERE id = $1
            """,
            row["id"], new_count, error_message[:8000],
        )
        self.stats["retried"] += 1
        logger.info(
            "DLQ id=%s source=%s entity=%s retry %d scheduled in %.0fs",
            row["id"], row["source"], row["entity_id"], new_count, delay,
        )

    async def _on_permanent(
        self, row: dict, error_message: str,
    ) -> None:
        """Mark row failed without further retries."""
        await self._pool.execute(
            """
            UPDATE dead_letter_queue
            SET status = 'failed',
                error_message = $2,
                last_attempt_at = NOW()
            WHERE id = $1
            """,
            row["id"], error_message[:8000],
        )
        self.stats["failed"] += 1
        logger.warning(
            "DLQ id=%s source=%s entity=%s permanent failure: %s",
            row["id"], row["source"], row["entity_id"], error_message[:200],
        )

    async def _process_row(self, row: dict) -> None:
        handler = self._handlers.get(row["source"])
        if handler is None:
            # Should not happen — _claim_batch only returns rows whose
            # source is in self._handlers. But guard anyway: release.
            await self._pool.execute(
                "UPDATE dead_letter_queue SET status='pending' WHERE id=$1",
                row["id"],
            )
            logger.warning(
                "DLQ source=%r row claimed but handler missing — released",
                row["source"],
            )
            return

        try:
            await asyncio.wait_for(
                handler(row),
                timeout=self.handler_timeout_seconds,
            )
        except PermanentError as e:
            await self._on_permanent(row, str(e))
        except asyncio.CancelledError:
            # Don't count cancellation as a retry — release to pending.
            await self._pool.execute(
                "UPDATE dead_letter_queue SET status='pending' WHERE id=$1",
                row["id"],
            )
            raise
        except Exception as e:  # noqa: BLE001 — handler is opaque
            await self._on_retry(row, f"{type(e).__name__}: {e}")
        else:
            await self._on_success(row["id"])
            self.stats["succeeded"] += 1

    # -- public ops -----------------------------------------------------

    async def run_once(self) -> int:
        """Claim + process one batch. Returns rows processed."""
        rows = await self._claim_batch()
        if not rows:
            return 0
        self.stats["scans"] += 1
        self.stats["claimed"] += len(rows)
        # Process sequentially. Could go concurrent but most handlers
        # touch shared state (rate-limiter / account-pool) so serial
        # is the safer default.
        for row in rows:
            await self._process_row(row)
        return len(rows)

    async def run_forever(self) -> None:
        """Loop: scan + process + sleep, until stop() is called."""
        logger.info(
            "DLQConsumer starting: handlers=%s, scan_interval=%.0fs, max_retries=%d",
            sorted(self._handlers.keys()), self.scan_interval_seconds,
            self.max_retries,
        )
        # Recover orphans at boot: rows a previous consumer flipped to
        # 'in_progress' then died on (container restart) sit stuck forever
        # otherwise — this is exactly how 83 rows accumulated 'in_progress' with
        # retry_count=0, never re-attempted. recover_orphans existed but was never
        # called; wire it in here and once per idle scan below.
        try:
            await self.recover_orphans()
        except Exception:
            logger.exception("DLQ boot orphan recovery failed")
        while not self._stop.is_set():
            try:
                # Sweep orphans each loop (cheap UPDATE) so a mid-batch crash
                # self-heals within ~stale_after instead of never.
                await self.recover_orphans()
                processed = await self.run_once()
                if processed == 0:
                    # Idle scan -> sleep full interval.
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(),
                            timeout=self.scan_interval_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass
                # Else loop immediately to drain pending work.
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("DLQConsumer scan failed; sleeping then retry")
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.scan_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
        logger.info("DLQConsumer stopped")

    def stop(self) -> None:
        self._stop.set()

    async def recover_orphans(self, stale_after_seconds: float = 300.0) -> int:
        """Flip in_progress rows older than stale_after to pending.

        Use when a consumer crashes mid-batch — its rows are stuck
        'in_progress' until manually cleared. Call periodically (e.g.
        once per scan in run_forever) or at boot.
        """
        result = await self._pool.execute(
            f"""
            UPDATE dead_letter_queue
            SET status = 'pending'
            WHERE status = 'in_progress'
              AND last_attempt_at < NOW() - INTERVAL '1 second' * {stale_after_seconds:.6f}
            """
        )
        try:
            n = int(result.split()[-1])
        except (ValueError, IndexError):
            n = 0
        if n > 0:
            self.stats["orphans_recovered"] += n
            logger.warning("DLQ recovered %d orphan in_progress rows", n)
        return n


__all__ = ["DLQConsumer", "PermanentError", "compute_next_retry"]
