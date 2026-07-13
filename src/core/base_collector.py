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
    # Provenance tag written to media_items.ingest_path (P2 review §3). Default is
    # the server-side cookie path; realtime messaging collectors override this to
    # 'messaging'. The browser-extension bridge (ig_ingest) sets 'extension' in its
    # own INSERT. See migration add_media_items_ingest_path.sql.
    INGEST_PATH: str = "headless"

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
        # Media reconciler: bounded, drive-gated, tombstoning re-download of
        # media_items whose backing file is gone (feature_gap_analysis #5). Gated
        # by COLLECTOR_RECOVER_MISSING; no-op/fast-path when disabled or a source
        # has already completed its refill. See src/core/reconciler.py.
        from .reconciler import Reconciler
        self.reconciler = Reconciler(self.SOURCE_NAME)
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
        self.reconciler.set_pool(pool)

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
                # Count the failed re-download so the reconciler can tombstone an
                # item that's permanently unavailable (expired/lost source asset).
                self.reconciler.record_failure(content_id)
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

        # Reset the reconciler's per-cycle counters (budget + auto-complete
        # tracking; see is_known). Each run() == one collect cycle.
        self.reconciler.reset_cycle()
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
            # Proactive reconciler sweep — re-download media_items whose file is
            # missing (drives refill independent of what collect() re-encountered).
            # Runs before finalize so _missing_seen reflects the true gap.
            await self.reconciler.sweep()
            # Reconciler bookkeeping: alert on an abnormal missing-rate, persist
            # tombstones, auto-complete the source's refill, rotate the shard.
            await self.reconciler.maybe_alert(len(self._known_ids))
            await self.reconciler.finalize_cycle()
            self.reconciler.advance_shard()
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
        # Load persisted reconciler state (done-flag, tombstones). If the source
        # already completed its refill, reconciler.active goes False (fast path).
        await self.reconciler.load_state()
        recover = self.reconciler.active
        try:
            async with self.pool.acquire() as conn:
                # Recovery needs file_path to stat the backing file; normal ops
                # only need the id set (cheaper, no extra column materialized).
                if recover:
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
                    if recover:
                        self.reconciler.note_known(cid, r["file_path"])
            if rows:
                logger.info("Seeded %d known %s content_ids from DB%s",
                            len(rows), self.SOURCE_NAME,
                            " [reconciler ON]" if recover else "")
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
        # Reconciler: a known id whose backing file is gone is treated as unknown
        # (bounded per cycle, tombstone-aware) so the collector re-downloads it.
        if self.reconciler.should_recover(content_id):
            return False
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
                                metadata: dict | None = None,
                                kind: str | None = None) -> bool:
        """Insert into media_items. Returns False on duplicate (same source+content_id).

        For profile_photo items, sha256 is auto-computed from the file on disk
        if not provided, since the analyzer needs hashes for identity matching.

        ``source_url`` CONTRACT (audit 2026-07-07, commits 9b0e7d6..8c81214):
          Every collector MUST pass a source_url representing the canonical,
          human-openable URL for the media's source page (video / post /
          profile / etc.) so unifiedanalyzer can trace a stored file back to
          its origin AND the media reconciler can re-fetch on drive corruption.

          For platforms with genuinely no public URL (WhatsApp media is
          mediaKey-encrypted and expiring; private Telegram DMs have no /c/
          form), use a stable URI scheme instead:
              whatsapp://<chat_jid>/<msg_id>
              tg://…  (currently we leave DMs NULL — matches
                       _build_telegram_source_url behaviour).

          Each collector implements a ``_build_<source>_source_url(item)``
          @staticmethod that derives the URL from item-dict fields
          (content_type + content_id + entity_name/id + any per-source
          extras like chat_username). Test cases live alongside the
          helper — see the 7 fix commits between 9b0e7d6 and 8c81214 for
          the pattern.
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

        # CROSS-COLLECTOR content dedup: for platforms scraped by BOTH the headless
        # collector and the browser extension (instagram/tiktok/lemon8) + the OSINT
        # sources, skip storing bytes we already have under a different content_id
        # (the two paths key content_ids differently). Messaging sources are EXCLUDED
        # — the same media legitimately appears in multiple chats there.
        if sha256 and self.SOURCE_NAME in (
            "instagram", "tiktok", "lemon8", "threads", "facebook", "x",
            "search", "website", "github", "strava",
        ):
            try:
                async with self.pool.acquire() as conn:
                    dup = await conn.fetchval(
                        "SELECT 1 FROM media_items WHERE source=$1 AND sha256=$2 LIMIT 1",
                        self.SOURCE_NAME, sha256,
                    )
                if dup:
                    try:
                        if file_path and Path(file_path).exists():
                            Path(file_path).unlink()
                    except Exception:
                        pass
                    logger.debug("cross-collector dup skipped: %s sha=%s", self.SOURCE_NAME, sha256[:12])
                    return False
            except Exception:
                pass

        # ATOMIC dedup (P2 review §3): `ON CONFLICT DO NOTHING` (no target) skips on
        # ANY unique violation — both the (source, content_id) index and the partial
        # unique (source, sha256) index (uq_media_source_sha256, in-scope sources).
        # This replaces the old racy SELECT-then-INSERT sha256 pre-check + the
        # UniqueViolationError catch: two concurrent tasks can no longer both insert
        # the same bytes under different content_ids. The advisory sha256 pre-check
        # above still runs first to delete the redundant blob in the common case;
        # the DB constraint is the backstop for the race it couldn't close.
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                INSERT INTO media_items
                    (source, entity_id, entity_name, content_type, content_id,
                     filename, file_path, file_size, width, height,
                     sha256, source_url, metadata, ingest_path, kind)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,$15)
                ON CONFLICT DO NOTHING
                """,
                self.SOURCE_NAME, entity_id, entity_name, content_type, content_id,
                filename, file_path, file_size, width, height,
                sha256, source_url, json.dumps(metadata, default=str) if metadata is not None else None,
                self.INGEST_PATH, kind,
            )
        # asyncpg returns the command tag, e.g. "INSERT 0 1" (stored) / "INSERT 0 0"
        # (a unique conflict skipped it).
        if status.endswith(" 1"):
            self._progress_count += 1
            return True
        logger.debug("Duplicate skipped (content_id or sha256): %s/%s", self.SOURCE_NAME, content_id)
        return False

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
