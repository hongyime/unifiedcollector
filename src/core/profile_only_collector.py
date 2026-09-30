"""ProfileOnlyCollector - queue-driven, one-shot profile enricher pattern.

New pattern for Do Next #3. Unlike existing BaseCollector subclasses,
which run a continuous ``worker --source <name>`` loop with reconciler,
checkpoint manager, and media firehose, a ProfileOnlyCollector:

- Reads targets from profile_probe_queue where source == SOURCE_NAME
  and status == 'pending'.
- Probes N per cycle (rate-limited).
- Writes one row into ``PROFILE_TABLE`` (per-platform).
- Marks queue rows as done / 404 / error.
- Does NOT download media, run reconciler, or accumulate a spider queue.

Deployment: all 5 (snapchat / paypal / airbnb / bluesky / pinterest) run
inside a single ``collector_profile_only`` container (256MB) doing a
round-robin over sources. See docker/docker-compose.yml + spec Do Next #3.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from abc import abstractmethod
from dataclasses import dataclass
from typing import Optional

from src.core.base_collector import BaseCollector

logger = logging.getLogger(__name__)


@dataclass
class ProfileResult:
    """Platform-specific probe output. `exists` mirrors HTTP 200 vs 404."""
    exists: bool
    username: str
    fields: dict  # keys matching the platform's <PROFILE_TABLE> columns
    raw_payload: Optional[dict] = None
    error: Optional[str] = None


class ProfileOnlyCollector(BaseCollector):
    """Queue-driven profile enricher. Subclasses fill in the platform-specific
    ``probe_profile`` and ``PROFILE_TABLE`` / ``SOURCE_NAME``.

    Env vars per platform (subclass supplies its own ``SOURCE_NAME``):
      - ``<SOURCE>_PROBE_ENABLED``  default 1
      - ``<SOURCE>_PROBE_BATCH``    default 10
      - ``<SOURCE>_PROBE_QPS``      default 0.5
      - ``<SOURCE>_PROBE_STALE_DAYS`` default 30
    """

    PROFILE_TABLE: str = ""
    PROBE_BATCH_DEFAULT: int = 10
    PROBE_QPS_DEFAULT: float = 0.5
    STALE_DAYS_DEFAULT: int = 30

    def __init__(self) -> None:
        super().__init__()
        if not self.PROFILE_TABLE:
            raise ValueError(f"{type(self).__name__} must set PROFILE_TABLE")

    # --- config helpers ---

    def _env(self, suffix: str, default: str) -> str:
        return os.getenv(f"{self.SOURCE_NAME.upper()}_PROBE_{suffix}", default)

    @property
    def enabled(self) -> bool:
        return self._env("ENABLED", "1") == "1"

    @property
    def batch_size(self) -> int:
        try:
            return int(self._env("BATCH", str(self.PROBE_BATCH_DEFAULT)))
        except ValueError:
            return self.PROBE_BATCH_DEFAULT

    @property
    def qps(self) -> float:
        try:
            return float(self._env("QPS", str(self.PROBE_QPS_DEFAULT)))
        except ValueError:
            return self.PROBE_QPS_DEFAULT

    # --- required abstract interface (from BaseCollector) is not used
    #     for profile-only; provide no-op implementations. ---

    async def collect(self, targets: list[str]):  # pragma: no cover
        # Queue-driven; collect() is not the entrypoint for this pattern.
        return

    async def download_media(self, item: dict):  # pragma: no cover
        return

    # --- platform-specific ---

    @abstractmethod
    async def probe_profile(self, username: str) -> ProfileResult:
        """Fetch profile info for a single username. Read-only.

        On network failure / rate-limit, raise or set ``result.error``.
        Never modify anything on the target platform.
        """

    # --- runtime ---

    async def run_cycle(self) -> dict:
        """Pop a batch of pending queue rows, probe each, write profile
        rows, and mark queue rows done/404/error."""
        summary = {"skipped": None, "processed": 0, "found": 0, "not_found": 0, "errors": 0}
        if not self.enabled:
            summary["skipped"] = "disabled"
            return summary
        if self.pool is None:
            summary["skipped"] = "no_pool"
            return summary

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, username
                FROM profile_probe_queue
                WHERE source = $1 AND status = 'pending'
                ORDER BY added_at ASC
                LIMIT $2
                """,
                self.SOURCE_NAME,
                self.batch_size,
            )
        if not rows:
            return summary

        interval = 1.0 / max(self.qps, 0.01)
        for row in rows:
            queue_id = row["id"]
            username = row["username"]
            summary["processed"] += 1

            try:
                result = await self.probe_profile(username)
            except Exception as exc:
                logger.warning("%s probe_profile(%s) failed: %s", self.SOURCE_NAME, username, exc)
                await self._mark_error(queue_id, str(exc)[:200])
                summary["errors"] += 1
                await asyncio.sleep(interval)
                continue

            if result.error and not result.exists:
                await self._mark_error(queue_id, result.error[:200])
                summary["errors"] += 1
                await asyncio.sleep(interval)
                continue

            if not result.exists:
                await self._mark_404(queue_id)
                summary["not_found"] += 1
                await asyncio.sleep(interval)
                continue

            await self._upsert_profile(username, result)
            await self._mark_done(queue_id)
            summary["found"] += 1
            self._progress_count += 1
            await asyncio.sleep(interval)

        return summary

    # --- queue mutation ---

    async def _mark_done(self, queue_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE profile_probe_queue SET status='done', probed_at=now() WHERE id=$1",
                queue_id,
            )

    async def _mark_404(self, queue_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE profile_probe_queue SET status='404', probed_at=now() WHERE id=$1",
                queue_id,
            )

    async def _mark_error(self, queue_id: int, message: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE profile_probe_queue
                SET status='error', probed_at=now(), error_message=$2
                WHERE id=$1
                """,
                queue_id,
                message,
            )

    # --- profile write. Subclass may override for platform-specific INSERT. ---

    async def _upsert_profile(self, username: str, result: ProfileResult) -> None:
        """Default INSERT: columns = fields.keys() + username + raw_payload.

        Subclasses may override for non-trivial cases (e.g. Bluesky's
        `did` requires a special path). The default handles the 4
        straightforward per-platform tables.
        """
        cols = ["username"] + list(result.fields.keys()) + ["raw_payload"]
        placeholders = [f"${i+1}" for i in range(len(cols))]
        values = [username] + list(result.fields.values()) + [
            json.dumps(result.raw_payload) if result.raw_payload else None
        ]

        update_pairs = [
            f"{c} = EXCLUDED.{c}" for c in cols if c != "username"
        ]
        sql = (
            f"INSERT INTO {self.PROFILE_TABLE} ({', '.join(cols)}, probed_at) "
            f"VALUES ({', '.join(placeholders)}, now()) "
            f"ON CONFLICT (username) DO UPDATE SET "
            f"{', '.join(update_pairs)}, probed_at=now()"
        )
        async with self.pool.acquire() as conn:
            await conn.execute(sql, *values)


async def enqueue(pool, source: str, username: str, enqueued_by: str) -> None:
    """Helper for other pipelines / operator UI to add probe targets."""
    if not username or not source:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO profile_probe_queue (source, username, enqueued_by)
            VALUES ($1, $2, $3)
            ON CONFLICT (source, username) DO NOTHING
            """,
            source,
            username,
            enqueued_by,
        )


__all__ = ["ProfileOnlyCollector", "ProfileResult", "enqueue"]
