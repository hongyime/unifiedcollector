import asyncio
import hashlib
import json
import logging
import os
import tempfile
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from .checkpoint import CheckpointManager
from .drive_check import check_drive, DRIVE_PATH
from .file_naming import build_filename, parse_filename
from .rate_limiter import AdaptiveRateLimiter
from .resilience import CircuitBreaker, wait_for_internet
from .user_agent import UserAgentPool

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """Base class for all source collectors."""

    SOURCE_NAME: str = ""
    USE_HUMAN_RATE_LIMITER: bool = False
    USE_ACCOUNT_POOL: bool = False

    def __init__(self):
        if not self.SOURCE_NAME:
            raise ValueError("Subclass must set SOURCE_NAME")

        self.pool = None
        if self.USE_HUMAN_RATE_LIMITER:
            from .human_rate_limiter import HumanLikeRateLimiter
            self.rate_limiter = HumanLikeRateLimiter()
        else:
            self.rate_limiter = AdaptiveRateLimiter()
        self.circuit_breaker = CircuitBreaker(failure_threshold=5)
        self.checkpoint = CheckpointManager(self.SOURCE_NAME)
        self.drive_ok = check_drive()
        self._stop = threading.Event()
        self.user_agents = UserAgentPool()
        self.account_pool = None
        self._known_ids: set[str] = set()
        # RECOVERY MODE (disaster refill, e.g. external media drive reformatted):
        # when COLLECTOR_RECOVER_MISSING is set, is_known() also verifies the
        # backing file still exists on disk. A surviving media_items row whose
        # file is gone is treated as UNKNOWN so the collector re-downloads it.
        # Non-destructive: rows are kept; ON CONFLICT refreshes them on refetch.
        # Off by default (normal ops keep the fast O(1) set-membership path).
        # TODO(reconciler): supersede this flag with src/core/reconciler.py
        # (feature_gap_analysis.md #5) for incremental tier1/tier2 reconciliation.
        self._recover_missing: bool = os.environ.get(
            "COLLECTOR_RECOVER_MISSING", ""
        ).lower() in ("1", "true", "yes")
        # content_id -> file_path, populated by _seed_known_ids when recovery is
        # on, so is_known() can stat the backing file. Empty in normal ops.
        self._known_paths: dict[str, str] = {}
        # Anti-starvation: cap how many missing-file re-downloads is_known()
        # releases per collect cycle. The forward pass re-downloads inline, so an
        # unbounded recovery would pin the cycle on old media and never advance
        # the live cursor. New content (not in _known_ids) is NEVER budgeted, so
        # live scraping is unaffected; only the refill of lost files is throttled.
        # 0 = unlimited. Reset to 0 at the start of each run() cycle.
        self._recover_budget = int(os.getenv("COLLECTOR_RECOVER_PER_CYCLE", "200"))
        self._recover_released = 0
        # Auto-complete: after this many consecutive cycles finding ZERO missing
        # files, the source's refill is done — persist that (recover_state table)
        # and drop back to fast mode, so COLLECTOR_RECOVER_MISSING never needs a
        # manual flip-off. 0 disables auto-complete (stays on until env removed).
        self._recover_done_after = int(os.getenv("COLLECTOR_RECOVER_DONE_CYCLES", "3"))
        self._recover_missing_seen = 0   # missing files detected this cycle
        self._clean_recover_cycles = 0   # consecutive cycles with 0 missing
        # Progress signal for the worker's zero-progress watchdog. Counts items
        # actually persisted (real INSERT into media_items, not dedup-skips).
        # The worker samples this before/after each collect cycle; a cycle that
        # had targets but did not advance this counter is a "zero-progress" cycle.
        self._progress_count: int = 0

    @property
    def progress_count(self) -> int:
        """Monotonic count of items actually persisted since process start."""
        return self._progress_count

    def set_pool(self, pool):
        self.pool = pool
        self.checkpoint.set_pool(pool)

    @property
    def media_dir(self) -> Path:
        return Path(DRIVE_PATH) / self.SOURCE_NAME

    # --- Abstract interface ---

    @abstractmethod
    async def collect(self, targets: list[str]):
        """Main collection method — fetch metadata and queue downloads."""

    @abstractmethod
    async def download_media(self, item: dict):
        """Download and save a single media item."""

    # --- Backfill interface ---

    async def get_backfill_items(self, batch_size: int) -> list[dict]:
        """Return up to batch_size items that need media downloaded.

        Each dict must have at minimum: entity_id, content_id, source_url.
        Additional keys (entity_name, content_type, metadata) are passed
        through to download_media().

        Default: returns [] (no backfill). Override in subclasses that have
        historical records missing media files.
        """
        return []

    async def run_backfill(self):
        """Fetch backfill items and download each via download_media().

        Called at the END of each collect() cycle by run(), after the main
        collection pass. Uses the existing DLQ for failures.
        """
        batch_size = int(os.getenv("BACKFILL_BATCH_SIZE", "100"))
        items = await self.get_backfill_items(batch_size)
        if not items:
            return 0

        downloaded = 0
        for item in items:
            if self._stop.is_set():
                break
            content_id = item.get("content_id", "")
            if self.is_known(content_id):
                continue
            try:
                await self.download_media(item)
                downloaded += 1
            except Exception as e:
                logger.warning("%s backfill failed %s: %s",
                               self.SOURCE_NAME, content_id, e)
                try:
                    await self.send_to_dlq(
                        item.get("entity_id", ""),
                        content_id,
                        str(e)[:500],
                    )
                except Exception:
                    logger.debug("%s: DLQ insert also failed for %s",
                                 self.SOURCE_NAME, content_id, exc_info=True)
        if downloaded:
            logger.info("%s: backfilled %d/%d items",
                        self.SOURCE_NAME, downloaded, len(items))
        return downloaded

    # --- Lifecycle ---

    async def run(self, targets: list[str]):
        """Entry point with all safety checks."""
        if not self.drive_ok:
            raise RuntimeError(f"Drive not mounted. Pausing {self.SOURCE_NAME}.")

        # Circuit breaker: skip entire cycle if open (too many recent failures).
        if not self.circuit_breaker.allow_request():
            logger.warning(
                "%s: circuit breaker OPEN — skipping collection cycle "
                "(will retry after recovery timeout)",
                self.SOURCE_NAME,
            )
            return

        self.media_dir.mkdir(parents=True, exist_ok=True)
        await self._seed_known_ids()
        await self.checkpoint.load_progress()
        await self.checkpoint.reset_if_stale()
        await self.checkpoint.mark_running()

        # Reset the per-cycle recover-missing counters (anti-starvation budget +
        # auto-complete tracking; see is_known). Each run() == one collect cycle.
        self._recover_released = 0
        self._recover_missing_seen = 0
        progress_before = self._progress_count

        logger.info("Starting %s collector (%d known items)", self.SOURCE_NAME, len(self._known_ids))
        try:
            await self.collect(targets)
            self.circuit_breaker.record_success()
            await self.run_backfill()
        except Exception as e:
            self.circuit_breaker.record_failure()
            await self._notify_run_error(e)
            raise
        else:
            await self._notify_run_summary(self._progress_count - progress_before)
            await self._update_recover_completion()
        finally:
            await self.checkpoint.mark_idle()
            logger.info("Stopped %s collector", self.SOURCE_NAME)

    # --- Telegram notifications (best-effort; never disturb collection) ---

    async def _notify_run_summary(self, collected: int):
        """Optional per-cycle summary. Off by default (COLLECTOR_NOTIFY_SUMMARIES)
        to avoid flooding the group during a large media refill — the scheduler's
        periodic heartbeat is the primary 'where things stand' signal."""
        if os.getenv("COLLECTOR_NOTIFY_SUMMARIES", "").lower() not in ("1", "true", "yes"):
            return
        try:
            from src.notifications import alerts
            await alerts.notify_collection_summary(
                self.SOURCE_NAME, {"collected": collected}
            )
        except Exception:
            logger.debug("notify_collection_summary failed", exc_info=True)

    async def _notify_run_error(self, error: Exception):
        try:
            from src.notifications import alerts
            await alerts.notify_error(self.SOURCE_NAME, error)
        except Exception:
            logger.debug("notify_error failed", exc_info=True)

    # --- Recover-missing auto-complete (persisted per source) ---

    _RECOVER_STATE_DDL = (
        "CREATE TABLE IF NOT EXISTS recover_state ("
        "source TEXT PRIMARY KEY, done_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )

    async def _recover_already_done(self) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(self._RECOVER_STATE_DDL)
                return bool(await conn.fetchval(
                    "SELECT true FROM recover_state WHERE source = $1",
                    self.SOURCE_NAME,
                ))
        except Exception:
            logger.debug("recover_state read failed", exc_info=True)
            return False

    async def _update_recover_completion(self):
        """After a successful cycle: if recovery is on and this cycle found no
        missing files, count it; after N consecutive clean cycles persist the
        source as done and drop to fast mode. Any missing file resets the streak."""
        if not self._recover_missing or self._recover_done_after <= 0:
            return
        if self._recover_missing_seen == 0:
            self._clean_recover_cycles += 1
            if self._clean_recover_cycles >= self._recover_done_after:
                try:
                    async with self.pool.acquire() as conn:
                        await conn.execute(self._RECOVER_STATE_DDL)
                        await conn.execute(
                            "INSERT INTO recover_state (source) VALUES ($1) "
                            "ON CONFLICT (source) DO NOTHING",
                            self.SOURCE_NAME,
                        )
                except Exception:
                    logger.debug("recover_state write failed", exc_info=True)
                self._recover_missing = False
                logger.info(
                    "%s: media refill complete (%d clean cycles) — "
                    "recover-missing auto-OFF",
                    self.SOURCE_NAME, self._clean_recover_cycles,
                )
        else:
            self._clean_recover_cycles = 0

    async def _seed_known_ids(self):
        """DB-first dedup seed (P3-3). Replaces the per-instance O(files) disk
        scan of media_dir, which slowed over time and raced across telegram's
        multi-workers sharing one media_dir. media_items UNIQUE(source,content_id)
        is the dedup authority; we seed the in-memory cache from it with a single
        indexed query. save_media_item still relies on ON CONFLICT, so a missed
        cache entry only costs one harmless insert attempt, never a duplicate.
        """
        if self.pool is None:
            return
        # Auto-complete: if this source already finished its refill in a prior
        # run, stay in fast mode regardless of the env flag.
        if self._recover_missing and await self._recover_already_done():
            self._recover_missing = False
            logger.info("%s: media refill already complete (persisted) — "
                        "recover-missing OFF", self.SOURCE_NAME)
        try:
            async with self.pool.acquire() as conn:
                # Recovery mode needs file_path to stat the backing file; normal
                # ops only need the id set (cheaper, no extra column materialized).
                if self._recover_missing:
                    rows = await conn.fetch(
                        "SELECT content_id, file_path FROM media_items "
                        "WHERE source = $1",
                        self.SOURCE_NAME,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT content_id FROM media_items WHERE source = $1",
                        self.SOURCE_NAME,
                    )
            for r in rows:
                cid = r["content_id"]
                if cid:
                    self._known_ids.add(cid)
                    if self._recover_missing:
                        self._known_paths[cid] = r["file_path"]
            if rows:
                logger.info("Seeded %d known %s content_ids from DB%s",
                            len(rows), self.SOURCE_NAME,
                            " [recover-missing ON]" if self._recover_missing else "")
        except Exception:
            logger.warning("%s: DB known-id seed failed; relying on ON CONFLICT",
                           self.SOURCE_NAME, exc_info=True)

    def _scan_existing_media(self):
        """DEPRECATED (P3-3): legacy disk scan, kept as a fallback only. Use
        _seed_known_ids() which is DB-backed and multi-worker safe."""
        count = 0
        for f in self.media_dir.iterdir():
            if f.is_file() and not f.name.endswith(".tmp"):
                parsed = parse_filename(f.name, source_name=self.SOURCE_NAME)
                if parsed and parsed["source"] == self.SOURCE_NAME:
                    self._known_ids.add(parsed["content_id"])
                    count += 1
        if count:
            logger.info("Disk scan found %d existing %s items", count, self.SOURCE_NAME)

    def is_known(self, content_id: str) -> bool:
        if content_id not in self._known_ids:
            return False
        # Recovery mode: a known id whose downloaded file no longer exists on
        # disk is treated as unknown so the collector re-downloads it. file_path
        # is the container path (e.g. /media/...), valid inside the worker.
        if self._recover_missing:
            fp = self._known_paths.get(content_id)
            if fp and not os.path.exists(fp):
                self._recover_missing_seen += 1  # drives auto-complete
                # File gone → eligible for re-download, but bound how many we
                # release per cycle so refill can't starve forward scraping.
                if self._recover_budget <= 0 or self._recover_released < self._recover_budget:
                    self._recover_released += 1
                    return False
                return True  # budget spent this cycle; retry on a later cycle
        return True

    def stop(self):
        self._stop.set()

    async def mark_target_collected(self, target_id: str):
        """Update collection_targets after successfully collecting a target."""
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    UPDATE collection_targets
                    SET collection_count = collection_count + 1,
                        last_collection_at = NOW(),
                        status = 'completed'
                    WHERE source = $1 AND target_id = $2
                """, self.SOURCE_NAME, target_id)
        except Exception as e:
            logger.debug("Failed to update collection_targets for %s/%s: %s", 
                        self.SOURCE_NAME, target_id, e)

    # --- File I/O ---

    def save_json(self, data: dict, filename: str) -> Path:
        """Save a dictionary as a JSON file atomically."""
        dest = self.media_dir / filename
        content = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        fd, tmp_path = tempfile.mkstemp(dir=self.media_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)
            logger.debug("Saved JSON %s", dest)
            return dest
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def save_file(self, data: bytes, filename: str, metadata: dict | None = None) -> Path:
        """Atomic write: temp file -> fsync -> rename into media_dir.
        Also saves a _metadata.json and _raw.json if metadata is provided.
        """
        dest = self.media_dir / filename
        fd, tmp_path = tempfile.mkstemp(dir=self.media_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)

            if metadata:
                stem = Path(filename).stem
                # Save processed metadata
                self.save_json(metadata, f"{stem}_metadata.json")
                # Save raw API response if available
                if "raw" in metadata:
                    self.save_json(metadata["raw"], f"{stem}_raw.json")

            parsed = parse_filename(filename, source_name=self.SOURCE_NAME)
            if parsed:
                self._known_ids.add(parsed["content_id"])
            logger.debug("Saved %s", dest)
            return dest
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def build_filename(self, entity_id: str, entity_name: str,
                       content_type: str, content_id: str,
                       timestamp=None, extension: str = "jpg") -> str:
        return build_filename(
            self.SOURCE_NAME, entity_id, entity_name,
            content_type, content_id, timestamp, extension,
        )

    @staticmethod
    def sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # --- DB helpers ---

    async def insert_media_item(self, *, entity_id: str, entity_name: str,
                                content_type: str, content_id: str,
                                filename: str, file_path: str,
                                file_size: int | None = None,
                                width: int | None = None, height: int | None = None,
                                sha256: str | None = None,
                                source_url: str | None = None,
                                metadata: dict | None = None) -> bool:
        """Insert into media_items. Returns False on duplicate (same source+content_id).

        For profile_photo items, sha256 is auto-computed from the file on disk
        if not provided, since the analyzer needs hashes for identity matching.
        """
        if sha256 is None and content_type == "profile_photo" and file_path:
            try:
                data = Path(file_path).read_bytes()
                sha256 = self.sha256_bytes(data)
            except Exception:
                logger.debug("auto-sha256 for profile_photo %s failed", file_path, exc_info=True)

        # Tier 5: best-effort EXIF GPS from the saved photo. Merged into the
        # metadata jsonb as `exif_gps`; None (the common case — platforms strip
        # EXIF) leaves metadata untouched. Pure-local, never raises.
        try:
            from .exif_gps import extract_gps, is_exif_enabled, _IMAGE_CONTENT_TYPES
            if (is_exif_enabled() and file_path
                    and content_type in _IMAGE_CONTENT_TYPES):
                gps = extract_gps(file_path)
                if gps:
                    metadata = dict(metadata or {})
                    metadata.setdefault("exif_gps", gps)
        except Exception:
            logger.debug("EXIF GPS hook failed for %s", file_path, exc_info=True)

        try:
            import asyncpg
        except ImportError:
            asyncpg = None  # type: ignore
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO media_items
                        (source, entity_id, entity_name, content_type, content_id,
                         filename, file_path, file_size, width, height,
                         sha256, source_url, metadata)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb)
                    """,
                    self.SOURCE_NAME, entity_id, entity_name, content_type, content_id,
                    filename, file_path, file_size, width, height,
                    sha256, source_url, json.dumps(metadata, default=str) if metadata is not None else None,
                )
            self._progress_count += 1
            return True
        except Exception as e:
            # Use the typed exception when available so we don't accidentally
            # swallow unrelated errors that happen to mention "unique" in their text.
            if asyncpg is not None and isinstance(e, getattr(asyncpg, "UniqueViolationError", ())):
                logger.debug("Duplicate skipped: %s/%s", self.SOURCE_NAME, content_id)
                return False
            raise

    async def send_to_dlq(self, entity_id: str, content_id: str, error: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                VALUES ($1, $2, $3, $4)
                """,
                self.SOURCE_NAME, entity_id, content_id, error,
            )

    # --- Helpers ---

    async def wait_rate_limit(self, domain: str | None = None):
        domain = domain or self.SOURCE_NAME
        delay = self.rate_limiter.get_delay(domain)
        if delay > 0:
            await asyncio.sleep(delay)

    async def ensure_internet(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, wait_for_internet,
            "8.8.8.8", 53, 3.0, 10.0, self._stop,
        )
