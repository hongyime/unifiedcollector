"""
shared/live_config.py — Live Config Tuning shared module.

This module provides the ParameterMeta dataclass, ConfigValidationError exception,
PARAMETER_REGISTRY, and ConfigOverlay class for runtime configuration management
across all seven Python services.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

logger = logging.getLogger(__name__)


@dataclass
class ParameterMeta:
    """Describes a single tunable parameter in the live config system."""

    key: str
    service: str
    python_type: type
    default: Any
    description: str
    min_value: float | None = None
    max_value: float | None = None
    options: list[str] | None = None
    requires_restart: bool = False
    multi_select: bool = False
    known_values: list[str] | None = None

    def __post_init__(self) -> None:
        if self.multi_select and self.options is None and self.known_values is None:
            raise ValueError(
                f"ParameterMeta '{self.key}' (service='{self.service}') has "
                "multi_select=True but neither 'options' nor 'known_values' is set. "
                "Provide at least one of them so the multiselect widget has choices."
            )


class ConfigValidationError(ValueError):
    """Raised when a supplied value fails type, range, or options validation."""


# ---------------------------------------------------------------------------
# PARAMETER_REGISTRY — single source of truth for all tunable parameters
# across all seven services.  Keyed by service name.
# ---------------------------------------------------------------------------

PARAMETER_REGISTRY: dict[str, list[ParameterMeta]] = {
    "processor_py": [
        ParameterMeta(
            key="PYTHON_LOG_LEVEL",
            service="processor_py",
            python_type=str,
            default="INFO",
            description="Python logging level for the processor service",
            options=["DEBUG", "INFO", "WARNING", "ERROR"],
        ),
        ParameterMeta(
            key="MAX_IMAGE_DIMENSION",
            service="processor_py",
            python_type=int,
            default=4096,
            description="Maximum pixel dimension (width or height) for processed images",
            min_value=100,
            max_value=4096,
        ),
        ParameterMeta(
            key="VIDEO_FRAME_RATE",
            service="processor_py",
            python_type=int,
            default=1,
            description="Frames per second extracted from video for processing",
            min_value=1,
            max_value=30,
        ),
        ParameterMeta(
            key="PHASH_DEDUP_THRESHOLD",
            service="processor_py",
            python_type=int,
            default=10,
            description="Perceptual hash Hamming distance threshold for deduplication (0 = exact match only)",
            min_value=0,
            max_value=64,
        ),
        ParameterMeta(
            key="FACE_MATCH_THRESHOLD",
            service="processor_py",
            python_type=float,
            default=0.6,
            description="Face similarity distance threshold; lower = stricter matching",
            min_value=0.0,
            max_value=1.0,
        ),
        ParameterMeta(
            key="FACE_UPSAMPLE_TIMES",
            service="processor_py",
            python_type=int,
            default=1,
            description="Number of times to upsample image before face detection (higher = detects smaller faces)",
            min_value=0,
            max_value=3,
        ),
        ParameterMeta(
            key="FINDINGS_HUB_BATCH_INTERVAL",
            service="processor_py",
            python_type=float,
            default=60.0,
            description="Seconds between findings batch flushes to the hub",
            min_value=0.1,
            max_value=60.0,
        ),
        ParameterMeta(
            key="FINDINGS_HUB_MIN_CONFIDENCE",
            service="processor_py",
            python_type=float,
            default=0.5,
            description="Minimum confidence score for a finding to be forwarded to the hub",
            min_value=0.0,
            max_value=1.0,
        ),
        ParameterMeta(
            key="MEDIA_REDOWNLOAD_ENABLED",
            service="processor_py",
            python_type=bool,
            default=False,
            description="Enable proactive re-download of expiring media before it becomes unavailable",
        ),
        ParameterMeta(
            key="MEDIA_REDOWNLOAD_LOOKAHEAD_HOURS",
            service="processor_py",
            python_type=int,
            default=2,
            description="Hours ahead of expiry to schedule media re-downloads",
            min_value=1,
            max_value=168,
        ),
        ParameterMeta(
            key="DB_POOL_SIZE",
            service="processor_py",
            python_type=int,
            default=5,
            description="SQLAlchemy connection pool size",
            min_value=1,
            max_value=50,
            requires_restart=True,
        ),
        ParameterMeta(
            key="DB_MAX_OVERFLOW",
            service="processor_py",
            python_type=int,
            default=10,
            description="Maximum overflow connections above DB_POOL_SIZE",
            min_value=0,
            max_value=50,
            requires_restart=True,
        ),
        ParameterMeta(
            key="DB_POOL_TIMEOUT",
            service="processor_py",
            python_type=int,
            default=30,
            description="Seconds to wait for a connection from the pool before raising an error",
            min_value=5,
            max_value=120,
            requires_restart=True,
        ),
        ParameterMeta(
            key="DB_POOL_RECYCLE",
            service="processor_py",
            python_type=int,
            default=1800,
            description="Seconds after which idle connections are recycled",
            min_value=60,
            max_value=7200,
            requires_restart=True,
        ),
        ParameterMeta(
            key="BROKER_TYPE",
            service="processor_py",
            python_type=str,
            default="redis",
            description="Message broker backend",
            options=["redis", "rabbitmq"],
            requires_restart=True,
        ),
        ParameterMeta(
            key="POSTGRES_SSL_MODE",
            service="processor_py",
            python_type=str,
            default="require",
            description="PostgreSQL TLS mode",
            options=["disable", "require", "verify-ca", "verify-full"],
            requires_restart=True,
        ),
    ],

    "collector": [
        ParameterMeta(
            key="LOG_LEVEL",
            service="collector",
            python_type=str,
            default="INFO",
            description="Python logging level for the collector service",
            options=["DEBUG", "INFO", "WARNING", "ERROR"],
        ),
        ParameterMeta(
            key="COLLECTOR_BACKFILL_REQ_PER_MIN",
            service="collector",
            python_type=int,
            default=5,
            description="Maximum backfill API requests per minute",
            min_value=1,
            max_value=600,
        ),
        ParameterMeta(
            key="COLLECTOR_BACKFILL_POLL_SECONDS",
            service="collector",
            python_type=int,
            default=30,
            description="Seconds between backfill polling cycles",
            min_value=1,
            max_value=300,
        ),
        ParameterMeta(
            key="COLLECTOR_MAX_BACKFILL_AGE_DAYS",
            service="collector",
            python_type=int,
            default=90,
            description="Maximum age in days of messages to include in backfill",
            min_value=1,
            max_value=365,
        ),
        ParameterMeta(
            key="COLLECTOR_DEDUP_TTL_SECONDS",
            service="collector",
            python_type=int,
            default=86400,
            description="TTL in seconds for deduplication cache entries",
            min_value=60,
            max_value=86400,
        ),
        ParameterMeta(
            key="SESSION_RISK_THRESHOLD",
            service="collector",
            python_type=float,
            default=0.8,
            description="Risk score above which a session is considered high-risk and cooled down",
            min_value=0.0,
            max_value=1.0,
        ),
        ParameterMeta(
            key="SESSION_COOLDOWN_SECONDS",
            service="collector",
            python_type=int,
            default=300,
            description="Seconds a high-risk session is paused before resuming collection",
            min_value=0,
            max_value=3600,
        ),
        ParameterMeta(
            key="SESSION_RISK_WINDOW_SECONDS",
            service="collector",
            python_type=int,
            default=3600,
            description="Rolling time window in seconds used to compute session risk score",
            min_value=60,
            max_value=86400,
        ),
        ParameterMeta(
            key="LANGUAGE_WHITELIST",
            service="collector",
            python_type=str,
            default="",
            description="Comma-separated BCP-47 language codes to collect (empty = all languages)",
            multi_select=True,
            known_values=[
                "en", "es", "fr", "de", "pt", "ar", "hi",
                "zh", "ru", "ja", "ko", "tr", "id", "vi",
            ],
        ),
    ],

    "media_archival": [
        ParameterMeta(
            key="MEDIA_CLEANUP_INTERVAL_HOURS",
            service="media_archival",
            python_type=int,
            default=24,
            description="Hours between media cleanup runs that remove expired files",
            min_value=1,
            max_value=168,
        ),
        ParameterMeta(
            key="MEDIA_RETENTION_DAYS",
            service="media_archival",
            python_type=int,
            default=90,
            description="Number of days to retain archived media before deletion",
            min_value=1,
            max_value=3650,
        ),
        ParameterMeta(
            key="MEDIA_REDOWNLOAD_ENABLED",
            service="media_archival",
            python_type=bool,
            default=False,
            description="Enable proactive re-download of expiring media before it becomes unavailable",
        ),
        ParameterMeta(
            key="MEDIA_REDOWNLOAD_LOOKAHEAD_HOURS",
            service="media_archival",
            python_type=int,
            default=2,
            description="Hours ahead of expiry to schedule media re-downloads",
            min_value=1,
            max_value=168,
        ),
        ParameterMeta(
            key="MEDIA_REDOWNLOAD_INTERVAL_SECONDS",
            service="media_archival",
            python_type=int,
            default=1800,
            description="Seconds between re-download worker cycles",
            min_value=60,
            max_value=86400,
        ),
        ParameterMeta(
            key="MEDIA_ARCHIVAL_POLL_SECONDS",
            service="media_archival",
            python_type=int,
            default=5,
            description="Seconds between archival worker polling cycles",
            min_value=1,
            max_value=300,
        ),
        ParameterMeta(
            key="MEDIA_ARCHIVAL_BATCH_SIZE",
            service="media_archival",
            python_type=int,
            default=50,
            description="Number of media items processed per archival batch",
            min_value=1,
            max_value=500,
        ),
        ParameterMeta(
            key="MEDIA_ARCHIVAL_MAX_DOWNLOAD_RETRIES",
            service="media_archival",
            python_type=int,
            default=3,
            description="Maximum number of download retry attempts before marking a media item as failed",
            min_value=1,
            max_value=10,
        ),
    ],

    "face_recognition": [
        ParameterMeta(
            key="FACE_MATCH_THRESHOLD",
            service="face_recognition",
            python_type=float,
            default=0.6,
            description="Face similarity distance threshold; lower = stricter matching",
            min_value=0.0,
            max_value=1.0,
        ),
        ParameterMeta(
            key="FACE_UPSAMPLE_TIMES",
            service="face_recognition",
            python_type=int,
            default=1,
            description="Number of times to upsample image before face detection (higher = detects smaller faces)",
            min_value=0,
            max_value=3,
        ),
        ParameterMeta(
            key="FACE_DETECTION_MODEL",
            service="face_recognition",
            python_type=str,
            default="hog",
            description="Face detection model: hog (fast, CPU) or cnn (accurate, GPU recommended)",
            options=["hog", "cnn"],
            requires_restart=True,
        ),
        ParameterMeta(
            key="FACE_PROCESSING_BATCH_SIZE",
            service="face_recognition",
            python_type=int,
            default=8,
            description="Number of images processed per face recognition batch",
            min_value=1,
            max_value=64,
        ),
        ParameterMeta(
            key="FACE_POLL_SECONDS",
            service="face_recognition",
            python_type=int,
            default=15,
            description="Seconds between face recognition worker polling cycles",
            min_value=1,
            max_value=300,
        ),
        ParameterMeta(
            key="MAX_IMAGE_DIMENSION",
            service="face_recognition",
            python_type=int,
            default=1600,
            description="Maximum pixel dimension (width or height) before resizing for face detection",
            min_value=100,
            max_value=4096,
        ),
        ParameterMeta(
            key="VIDEO_FRAME_RATE",
            service="face_recognition",
            python_type=int,
            default=1,
            description="Frames per second extracted from regular video for face processing",
            min_value=1,
            max_value=30,
        ),
        ParameterMeta(
            key="VIDEO_NOTE_FRAME_RATE",
            service="face_recognition",
            python_type=int,
            default=2,
            description="Frames per second extracted from video notes for face processing",
            min_value=1,
            max_value=30,
        ),
        ParameterMeta(
            key="PHASH_DEDUP_THRESHOLD",
            service="face_recognition",
            python_type=int,
            default=10,
            description="Perceptual hash Hamming distance threshold for deduplication (0 = exact match only)",
            min_value=0,
            max_value=64,
        ),
        ParameterMeta(
            key="FINDINGS_MAX_PER_HOUR",
            service="face_recognition",
            python_type=int,
            default=30,
            description="Maximum number of findings published per hour (rate limit)",
            min_value=1,
            max_value=10000,
        ),
        ParameterMeta(
            key="FINDINGS_SEND_DELAY",
            service="face_recognition",
            python_type=float,
            default=3.0,
            description="Seconds to wait between sending individual findings",
            min_value=0.0,
            max_value=60.0,
        ),
        ParameterMeta(
            key="FINDINGS_MIN_CONFIDENCE",
            service="face_recognition",
            python_type=float,
            default=0.5,
            description="Minimum confidence score for a finding to be published",
            min_value=0.0,
            max_value=1.0,
        ),
        ParameterMeta(
            key="FACE_BIOMETRIC_SEMAPHORE",
            service="face_recognition",
            python_type=int,
            default=1,
            description="Maximum concurrent biometric processing tasks (controls CPU/GPU load)",
            min_value=1,
            max_value=32,
            requires_restart=True,
        ),
    ],

    "user_intelligence": [
        ParameterMeta(
            key="USER_INTEL_BATCH_SIZE",
            service="user_intelligence",
            python_type=int,
            default=500,
            description="Number of user records processed per intelligence analysis batch",
            min_value=1,
            max_value=500,
        ),
        ParameterMeta(
            key="USER_INTEL_POLL_INTERVAL_SEC",
            service="user_intelligence",
            python_type=int,
            default=10,
            description="Seconds between user intelligence worker polling cycles",
            min_value=1,
            max_value=300,
        ),
        ParameterMeta(
            key="TRACKED_FIELDS",
            service="user_intelligence",
            python_type=str,
            default="display_name,push_name,business_name,phone_number,is_business,is_verified,profile_photo",
            description="Comma-separated list of user profile fields to track for change detection",
            multi_select=True,
            known_values=[
                "display_name", "push_name", "business_name",
                "phone_number", "is_business", "is_verified",
                "profile_photo", "about", "status",
            ],
        ),
    ],

    "link_discovery": [
        ParameterMeta(
            key="LINK_DISCOVERY_BATCH_SIZE",
            service="link_discovery",
            python_type=int,
            default=200,
            description="Number of links processed per discovery batch",
            min_value=1,
            max_value=500,
        ),
        ParameterMeta(
            key="LINK_DISCOVERY_POLL_INTERVAL_SEC",
            service="link_discovery",
            python_type=int,
            default=10,
            description="Seconds between link discovery worker polling cycles",
            min_value=1,
            max_value=300,
        ),
        ParameterMeta(
            key="LINK_DISCOVERY_MAX_JOINS_PER_HOUR",
            service="link_discovery",
            python_type=int,
            default=3,
            description="Maximum number of group join attempts per hour",
            min_value=1,
            max_value=1000,
        ),
        ParameterMeta(
            key="LINK_DISCOVERY_JOIN_DELAY_SECONDS",
            service="link_discovery",
            python_type=float,
            default=120.0,
            description="Seconds to wait between consecutive group join attempts",
            min_value=0.0,
            max_value=3600.0,
        ),
    ],

    "bulk_sender": [
        ParameterMeta(
            key="BULK_SENDER_INTERNAL_MIN_DELAY",
            service="bulk_sender",
            python_type=float,
            default=2.0,
            description="Minimum seconds between messages sent to internal targets",
            min_value=0.0,
            max_value=60.0,
        ),
        ParameterMeta(
            key="BULK_SENDER_EXTERNAL_MIN_DELAY",
            service="bulk_sender",
            python_type=float,
            default=8.0,
            description="Minimum seconds between messages sent to external targets",
            min_value=0.0,
            max_value=60.0,
        ),
        ParameterMeta(
            key="BULK_SENDER_EXTERNAL_MAX_PER_HOUR",
            service="bulk_sender",
            python_type=int,
            default=30,
            description="Maximum number of messages sent to external targets per hour",
            min_value=1,
            max_value=10000,
        ),
        ParameterMeta(
            key="BULK_SENDER_MAX_EXTERNAL_TARGETS",
            service="bulk_sender",
            python_type=int,
            default=20,
            description="Maximum number of distinct external targets per bulk send job",
            min_value=1,
            max_value=10000,
        ),
        ParameterMeta(
            key="BULK_SENDER_MEMBERSHIP_MIN_AGE_HOURS",
            service="bulk_sender",
            python_type=int,
            default=48,
            description="Minimum group membership age in hours before a target is eligible for bulk send",
            min_value=0,
            max_value=8760,
        ),
        ParameterMeta(
            key="BULK_SENDER_POLL_INTERVAL_SEC",
            service="bulk_sender",
            python_type=int,
            default=5,
            description="Seconds between bulk sender worker polling cycles",
            min_value=1,
            max_value=300,
        ),
    ],
}


# ---------------------------------------------------------------------------
# ConfigOverlay — wraps a pydantic BaseConfig instance and maintains an
# in-process _live dict of Redis overrides, exposing a merged view.
# ---------------------------------------------------------------------------

class ConfigOverlay:
    """
    Wraps a service's pydantic BaseConfig instance and maintains an in-process
    overlay of Redis-sourced overrides.  The poll loop (added in task 2) is
    responsible for keeping _live in sync with Redis.
    """

    def __init__(
        self,
        settings: Any,
        service_name: str,
        redis_url: str,
        poll_interval_seconds: int = 15,
    ) -> None:
        self._settings = settings
        self.service_name = service_name
        self._redis_url = redis_url
        self.poll_interval_seconds = poll_interval_seconds

        self._live: dict[str, Any] = {}
        self._redis = None
        self._poll_task = None
        self._connection_error_streak = 0
        self._missing_settings_keys_warned: set[str] = set()

        if aioredis is not None:
            try:
                self._redis = aioredis.Redis.from_url(redis_url, decode_responses=True)
            except Exception as exc:
                logger.warning(
                    "ConfigOverlay: failed to create Redis client from %r: %s",
                    redis_url,
                    exc,
                )
        else:
            logger.warning(
                "ConfigOverlay: redis.asyncio is not installed; "
                "live config will use env defaults only."
            )

    # ------------------------------------------------------------------
    # Core read interface
    # ------------------------------------------------------------------

    def get_env_default(self, key: str) -> Any:
        """Return the env/default value for key from settings or registry fallback."""
        if hasattr(self._settings, key):
            return getattr(self._settings, key)

        meta = self.schema.get(key)
        if meta is not None:
            if key not in self._missing_settings_keys_warned:
                logger.warning(
                    "ConfigOverlay.get_env_default: %r missing on settings for service %r; "
                    "falling back to registry default %r.",
                    key,
                    self.service_name,
                    meta.default,
                )
                self._missing_settings_keys_warned.add(key)
            return meta.default

        raise AttributeError(
            f"{type(self._settings).__name__!r} object has no attribute {key!r}"
        )

    def get(self, key: str) -> Any:
        """Return the live value for key: Redis override if present, else env default."""
        if key in self._live:
            return self._live[key]
        return self.get_env_default(key)

    def set_local(self, key: str, value: Any) -> None:
        """Apply a value in-process only (used internally by the poll loop)."""
        self._live[key] = value

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @property
    def schema(self) -> dict[str, ParameterMeta]:
        """Return {key: ParameterMeta} for all tunable parameters of this service."""
        return {meta.key: meta for meta in PARAMETER_REGISTRY.get(self.service_name, [])}

    # ------------------------------------------------------------------
    # Merged view
    # ------------------------------------------------------------------

    def get_all(self) -> dict[str, Any]:
        """Return a merged dict of all parameters: Redis overrides take precedence."""
        result: dict[str, Any] = {}
        for key in self.schema:
            if key in self._live:
                result[key] = self._live[key]
            else:
                result[key] = self.get_env_default(key)
        return result

    # ------------------------------------------------------------------
    # Write interface
    # ------------------------------------------------------------------

    async def push(self, key: str, raw_value: str) -> None:
        """Validate raw_value and write it to the Redis hash for this service.

        Raises ConfigValidationError if:
        - key is not registered for this service
        - raw_value cannot be coerced to the parameter's python_type
        - coerced value is outside [min_value, max_value]
        - coerced value is not in the options list
        - Redis is not available
        """
        meta = self.schema.get(key)
        if meta is None:
            raise ConfigValidationError(
                f"Unknown parameter: {key!r} for service {self.service_name!r}"
            )

        # --- Type coercion ---
        if meta.python_type is bool:
            if raw_value.lower() in ("true", "1", "yes"):
                coerced: Any = True
            elif raw_value.lower() in ("false", "0", "no"):
                coerced = False
            else:
                raise ConfigValidationError(
                    f"Invalid bool value {raw_value!r} for {key!r}; "
                    "expected one of: true, false, 1, 0, yes, no"
                )
        elif meta.python_type is int:
            try:
                coerced = int(raw_value)
            except ValueError:
                raise ConfigValidationError(
                    f"Invalid int value {raw_value!r} for {key!r}"
                )
        elif meta.python_type is float:
            try:
                coerced = float(raw_value)
            except ValueError:
                raise ConfigValidationError(
                    f"Invalid float value {raw_value!r} for {key!r}"
                )
        else:
            # str — use as-is
            coerced = raw_value

        # --- Range check ---
        if meta.min_value is not None and coerced < meta.min_value:
            raise ConfigValidationError(
                f"Value {coerced!r} for {key!r} is below minimum {meta.min_value}"
            )
        if meta.max_value is not None and coerced > meta.max_value:
            raise ConfigValidationError(
                f"Value {coerced!r} for {key!r} exceeds maximum {meta.max_value}"
            )

        # --- Options check ---
        if meta.options is not None and str(coerced) not in meta.options:
            raise ConfigValidationError(
                f"Value {coerced!r} for {key!r} is not in allowed options: {meta.options}"
            )

        # --- Write to Redis ---
        if self._redis is None:
            raise ConfigValidationError("Redis not available")

        await self._redis.hset(f"live_config:{self.service_name}", key, str(coerced))

    async def reset(self, key: str) -> None:
        """Remove a live override for key from the Redis hash.

        The in-process _live dict is NOT updated here — the poll loop will
        drop the key on its next cycle once it no longer appears in Redis.

        If Redis is unavailable, logs a warning and returns gracefully.
        """
        if self._redis is None:
            logger.warning(
                "ConfigOverlay.reset: Redis not available; cannot reset %r for service %r",
                key,
                self.service_name,
            )
            return

        await self._redis.hdel(f"live_config:{self.service_name}", key)

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def start_poll_loop(self) -> None:
        """Start the background poll loop.

        If Redis is not available, logs a warning and returns immediately
        (graceful degradation — Req 3.1).  Otherwise performs an initial
        HGETALL before spawning the background task (Req 11.3).
        """
        if self._redis is None:
            logger.warning(
                "ConfigOverlay.start_poll_loop: Redis not available for service %r; "
                "live config will use env defaults only.",
                self.service_name,
            )
            return

        # Initial load before the background task starts (Req 11.3)
        await self._poll_once()

        self._poll_task = asyncio.create_task(self._run_poll_loop())

    async def stop_poll_loop(self) -> None:
        """Cancel the background poll task and wait for it to finish cleanly."""
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await asyncio.shield(self._poll_task)
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_poll_loop(self) -> None:
        """Private: loop forever, polling Redis then sleeping."""
        try:
            while True:
                await self._poll_once()
                await asyncio.sleep(self.poll_interval_seconds)
        except asyncio.CancelledError:
            raise  # clean shutdown

    async def _poll_once(self) -> None:
        """Private: fetch HGETALL and update _live atomically.

        Handles:
        - ConnectionError → log warning once per streak, preserve _live (Req 3.2, 3.3)
        - Unknown keys → skip (Req 2.3)
        - requires_restart keys → skip (Req 8.4)
        - Coercion failure → log error, skip key (Req 2.5)
        - Keys absent from Redis → remove from _live (Req 2.4)
        """
        redis_key = f"live_config:{self.service_name}"
        try:
            raw_map: dict[str, str] = await self._redis.hgetall(redis_key)
        except Exception as exc:
            # Treat any connection-level error as a streak failure (Req 3.2)
            self._connection_error_streak += 1
            if self._connection_error_streak == 1:
                logger.warning(
                    "ConfigOverlay._poll_once: Redis connection error for service %r "
                    "(streak=%d): %s — preserving existing _live values.",
                    self.service_name,
                    self._connection_error_streak,
                    exc,
                )
            # Preserve existing _live values unchanged
            return

        # Successful fetch — reset streak (Req 3.4)
        self._connection_error_streak = 0

        schema = self.schema
        new_live: dict[str, Any] = {}

        for key, raw_value in raw_map.items():
            meta = schema.get(key)
            if meta is None:
                # Unknown key — skip (Req 2.3)
                continue

            if meta.requires_restart:
                # Do NOT apply restart-required params to _live (Req 8.4)
                continue

            # Coerce value to the declared python_type
            try:
                if meta.python_type is bool:
                    if raw_value.lower() in ("true", "1", "yes"):
                        coerced: Any = True
                    elif raw_value.lower() in ("false", "0", "no"):
                        coerced = False
                    else:
                        raise ValueError(f"cannot coerce {raw_value!r} to bool")
                elif meta.python_type is int:
                    coerced = int(raw_value)
                elif meta.python_type is float:
                    coerced = float(raw_value)
                else:
                    coerced = raw_value  # str — use as-is
            except (ValueError, TypeError) as exc:
                logger.error(
                    "ConfigOverlay._poll_once: coercion failure for key %r "
                    "(service=%r, value=%r): %s — skipping.",
                    key,
                    self.service_name,
                    raw_value,
                    exc,
                )
                continue

            new_live[key] = coerced

        # Atomically replace _live (Req 2.4 — absent keys are dropped)
        self._live = new_live
