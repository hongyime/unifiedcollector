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

    # --- Lifecycle ---

    async def run(self, targets: list[str]):
        """Entry point with all safety checks."""
        if not self.drive_ok:
            raise RuntimeError(f"Drive not mounted. Pausing {self.SOURCE_NAME}.")

        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._scan_existing_media()
        await self.checkpoint.load_progress()
        await self.checkpoint.reset_if_stale()
        await self.checkpoint.mark_running()

        logger.info("Starting %s collector (%d known items on disk)", self.SOURCE_NAME, len(self._known_ids))
        try:
            await self.collect(targets)
        finally:
            await self.checkpoint.mark_idle()
            logger.info("Stopped %s collector", self.SOURCE_NAME)

    def _scan_existing_media(self):
        """Disk-first dedup: scan media_dir for existing files and build _known_ids set."""
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
        return content_id in self._known_ids

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
                        status = 'active'
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
        """Insert into media_items. Returns False on duplicate (same source+content_id)."""
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
