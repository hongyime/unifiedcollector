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

Deployment: each source has its own container and its own collector class.
Compose starts ``profile_only_runner --source <name>``. The shared piece is
this base class plus that runner, not one process for all five.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from abc import abstractmethod
from dataclasses import dataclass
from typing import Optional

from src.core.base_collector import BaseCollector

logger = logging.getLogger(__name__)

_VERIFIED_BADGE_HTML_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:is_?verified|verified|is_?blue_?verified|blue_?verified|"
    r"isVerifiedMerchant|verifiedMerchant)[\"']?\s*[:=]\s*(?:true|1)(?![A-Za-z0-9_])"
    r"|aria-label\s*=\s*[\"'][^\"']*\bverified\b[^\"']*[\"']"
    r"|title\s*=\s*[\"'][^\"']*\bverified\b[^\"']*[\"']",
    re.IGNORECASE,
)

_FOLLOW_GRAPH_PROBE_SOURCES = frozenset({"snapchat", "pinterest"})
_PUBLIC_DISCOVERY_PLATFORMS = frozenset({"airbnb", "pinterest", "snapchat"})
_FOLLOW_GRAPH_TARGET_BATCH = 10

PROFILE_PROBE_MAX_ATTEMPTS_DEFAULT = 5
PROFILE_PROBE_BACKOFF_BASE_SECONDS_DEFAULT = 3600
PROFILE_PROBE_BACKOFF_CAP_SECONDS = 86400

# probe_profile() implementations mostly swallow transport failures into
# ProfileResult.error strings ("http:TimeoutException", "http:503"); only the
# raised path sees the original exception type.
_TRANSIENT_ERROR_MARKERS = ("timeout", "connect", "network", "protocol", "readerror")


def _is_transient_probe_error(exc: BaseException | None, message: str) -> bool:
    """True when a probe failure is worth retrying with backoff."""
    if exc is not None:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
            return True
        if any(marker in type(exc).__name__.lower() for marker in _TRANSIENT_ERROR_MARKERS):
            return True
    text = (message or "").lower()
    if "http:" in text:
        detail = text.split("http:", 1)[1].split(":", 1)[0].strip()
        if detail.isdigit():
            code = int(detail)
            return code == 429 or code >= 500
        return any(marker in detail for marker in _TRANSIENT_ERROR_MARKERS)
    # Challenge pages, malformed payloads, unexpected redirects: bounded retry
    # is safer than permanently dropping a possibly-real target.
    return True


_FOLLOW_GRAPH_SOURCE_SQL = """
    WITH followed AS (
        SELECT DISTINCT ON (LOWER(BTRIM(f.target_username)))
               LOWER(BTRIM(f.target_username)) AS username,
               f.platform AS origin_platform,
               f.last_seen
          FROM follow_edges f
         WHERE f.direction='following'
               AND f.platform IN ('github','instagram','x','tiktok','threads','pinterest','snapchat')
               AND BTRIM(COALESCE(f.target_username,'')) ~ '^[A-Za-z0-9._-]{1,100}$'
           AND NOT EXISTS (
                SELECT 1 FROM instagram_profiles p
                 WHERE f.platform='instagram' AND p.is_verified IS TRUE
                   AND (p.platform_user_id::text=f.target_uid
                        OR LOWER(p.username)=LOWER(BTRIM(f.target_username)))
           )
           AND NOT EXISTS (
                SELECT 1 FROM x_profiles p
                 WHERE f.platform='x' AND p.is_verified IS TRUE
                   AND (p.platform_user_id::text=f.target_uid
                        OR LOWER(p.username)=LOWER(BTRIM(f.target_username)))
           )
           AND NOT EXISTS (
                SELECT 1 FROM tiktok_profiles p
                 WHERE f.platform='tiktok' AND p.is_verified IS TRUE
                   AND (p.platform_user_id::text=f.target_uid
                        OR LOWER(p.username)=LOWER(BTRIM(f.target_username)))
           )
         ORDER BY LOWER(BTRIM(f.target_username)), f.last_seen DESC NULLS LAST
    ), promoted AS (
        UPDATE profile_probe_queue q
           SET enqueued_by='followed:' || followed.origin_platform
          FROM followed
         WHERE q.source=$1 AND q.status='pending'
           AND LOWER(BTRIM(q.username, '@'))=followed.username
           AND COALESCE(q.enqueued_by,'') NOT LIKE 'followed:%'
        RETURNING q.id
    )
    INSERT INTO profile_probe_queue (source, username, enqueued_by)
    SELECT $1, followed.username, 'followed:' || followed.origin_platform
      FROM followed
     WHERE NOT EXISTS (
            SELECT 1 FROM profile_probe_queue q
             WHERE q.source=$1
               AND LOWER(BTRIM(q.username, '@'))=followed.username
     )
     ORDER BY followed.last_seen DESC NULLS LAST, followed.username
     LIMIT GREATEST(0, $2 - (
            SELECT count(*) FROM profile_probe_queue q
             WHERE q.source=$1 AND q.status='pending'
               AND q.enqueued_by LIKE 'followed:%'
     ))
    ON CONFLICT (source, username) DO NOTHING
"""


@dataclass
class ProfileResult:
    """Platform-specific probe output. `exists` mirrors HTTP 200 vs 404."""
    exists: bool
    username: str
    fields: dict  # keys matching the platform's <PROFILE_TABLE> columns
    raw_payload: Optional[dict] = None
    error: Optional[str] = None
    excluded_verified: bool = False


class ProfileOnlyCollector(BaseCollector):
    """Queue-driven profile enricher. Subclasses fill in the platform-specific
    ``probe_profile`` and ``PROFILE_TABLE`` / ``SOURCE_NAME``.

    Env vars per platform (subclass supplies its own ``SOURCE_NAME``):
      - ``<SOURCE>_PROBE_ENABLED``  default 1
      - ``<SOURCE>_PROBE_BATCH``    default 10
      - ``<SOURCE>_PROBE_QPS``      default 0.5
      - ``<SOURCE>_PROBE_STALE_DAYS`` default 30
      - ``NATIVE_PUBLIC_DISCOVERY_ENABLED`` default 0 for Airbnb/Pinterest/Snapchat
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

    @property
    def probe_max_attempts(self) -> int:
        try:
            return max(
                1,
                int(os.getenv(
                    "PROFILE_PROBE_MAX_ATTEMPTS",
                    str(PROFILE_PROBE_MAX_ATTEMPTS_DEFAULT),
                )),
            )
        except ValueError:
            return PROFILE_PROBE_MAX_ATTEMPTS_DEFAULT

    @property
    def probe_backoff_base_seconds(self) -> int:
        try:
            return max(
                1,
                int(os.getenv(
                    "PROFILE_PROBE_BACKOFF_BASE_SECONDS",
                    str(PROFILE_PROBE_BACKOFF_BASE_SECONDS_DEFAULT),
                )),
            )
        except ValueError:
            return PROFILE_PROBE_BACKOFF_BASE_SECONDS_DEFAULT

    # --- required abstract interface (from BaseCollector) is not used
    #     for profile-only; provide no-op implementations. ---

    async def collect(self, targets: list[str]):  # pragma: no cover
        # Queue-driven; collect() is not the entrypoint for this pattern.
        return

    async def download_media(self, item: dict):  # pragma: no cover
        return

    # --- platform-specific ---

    @staticmethod
    def _page_has_explicit_verified_badge(html: str) -> bool:
        """Only match source-provided verification flags or badge labels."""
        return bool(_VERIFIED_BADGE_HTML_RE.search(html or ""))

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
        summary = {
            "skipped": None, "processed": 0, "found": 0,
            "not_found": 0, "errors": 0, "excluded_verified": 0,
        }
        if not self.enabled:
            summary["skipped"] = "disabled"
            return summary
        if self.pool is None:
            summary["skipped"] = "no_pool"
            return summary

        allow_public_discovery = (
            self.SOURCE_NAME not in _PUBLIC_DISCOVERY_PLATFORMS
            or self._env("NATIVE_PUBLIC_DISCOVERY_ENABLED", "0").strip().lower()
            in {"1", "true", "yes", "on"}
        )

        async with self.pool.acquire() as conn:
            if self.SOURCE_NAME in _FOLLOW_GRAPH_PROBE_SOURCES:
                await conn.execute(
                    _FOLLOW_GRAPH_SOURCE_SQL,
                    self.SOURCE_NAME,
                    _FOLLOW_GRAPH_TARGET_BATCH,
                )
            # Native targets live in a second queue. Keep spider/public profile
            # probes parked until followed targets from either queue are drained.
            rows = await conn.fetch(
                """
                SELECT id, username
                FROM profile_probe_queue
                WHERE source = $1 AND status = 'pending'
                  AND (next_probe_at IS NULL OR next_probe_at <= NOW())
                  AND (
                      enqueued_by LIKE 'followed:%'
                      OR (
                          $3::boolean
                          AND
                          NOT EXISTS (
                              SELECT 1 FROM profile_probe_queue preferred
                               WHERE preferred.source=$1
                                 AND preferred.status='pending'
                                 AND preferred.enqueued_by LIKE 'followed:%'
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM browser_native_scrape_queue native_preferred
                               WHERE native_preferred.platform=$1
                                 AND (
                                      ($1 IN ('pinterest','snapchat')
                                       AND native_preferred.source='followed_handle_candidate')
                                      OR ($1='airbnb'
                                          AND native_preferred.source='followed_account_link')
                                 )
                                 AND native_preferred.status IN ('pending','claimed')
                          )
                      )
                  )
                ORDER BY
                    CASE
                        WHEN enqueued_by LIKE 'followed:%' THEN 0
                        WHEN enqueued_by='spider_discovered_links' THEN 1
                        ELSE 2
                    END,
                    added_at ASC
                LIMIT $2
                """,
                self.SOURCE_NAME,
                self.batch_size,
                allow_public_discovery,
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
                message = str(exc)[:200]
                await self._mark_attempt(
                    self.SOURCE_NAME,
                    username,
                    message,
                    _is_transient_probe_error(exc, message),
                )
                summary["errors"] += 1
                await asyncio.sleep(interval)
                continue

            if result.error and not result.exists:
                await self._mark_attempt(
                    self.SOURCE_NAME,
                    username,
                    result.error[:200],
                    _is_transient_probe_error(None, result.error),
                )
                summary["errors"] += 1
                await asyncio.sleep(interval)
                continue

            if not result.exists:
                await self._mark_404(queue_id)
                summary["not_found"] += 1
                await asyncio.sleep(interval)
                continue

            if result.excluded_verified:
                await self._mark_done(queue_id)
                summary["excluded_verified"] += 1
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

    async def _mark_attempt(
        self, source: str, username: str, message: str, transient: bool,
    ) -> None:
        """Record one probe failure; retry transient errors with backoff.

        Transient failures re-enter 'pending' until ``probe_max_attempts`` is
        reached. Non-transient failures go straight to terminal 'error'.
        """
        async with self.pool.acquire() as conn:
            attempts = await conn.fetchval(
                """
                UPDATE profile_probe_queue
                SET attempts = attempts + 1, probed_at = now(), error_message = $3
                WHERE source = $1 AND username = $2
                RETURNING attempts
                """,
                source,
                username,
                message,
            )
            if attempts is None:
                return
            if transient and attempts < self.probe_max_attempts:
                delay = min(
                    self.probe_backoff_base_seconds * 2 ** (attempts - 1),
                    PROFILE_PROBE_BACKOFF_CAP_SECONDS,
                )
                await conn.execute(
                    """
                    UPDATE profile_probe_queue
                    SET status='pending',
                        next_probe_at = now() + ($3 * interval '1 second')
                    WHERE source = $1 AND username = $2
                    """,
                    source,
                    username,
                    delay,
                )
            else:
                await conn.execute(
                    """
                    UPDATE profile_probe_queue
                    SET status='error', next_probe_at = NULL
                    WHERE source = $1 AND username = $2
                    """,
                    source,
                    username,
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
