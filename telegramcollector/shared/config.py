from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, Any, List, Dict
import logging as _logging
import os

try:
    import psycopg as _psycopg
except Exception:  # pragma: no cover - optional dependency in some test contexts
    _psycopg = None

_config_logger = _logging.getLogger(__name__)

_BOOTSTRAP_ENV_KEYS = {
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_DB",
    "REDIS_PASSWORD",
    "CONFIG_STORE_ENCRYPTION_KEY",
}


def _is_truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def load_db_config_snapshot() -> Dict[str, str]:
    """Load effective config values from DB for runtime startup hydration."""
    if _is_truthy(os.getenv("CONFIG_STORE_DISABLE_SNAPSHOT")):
        return {}

    if _psycopg is None:
        return {}

    db_host = os.getenv("DB_HOST", "postgres")
    db_port = int(os.getenv("DB_PORT", "5432"))
    db_name = os.getenv("DB_NAME", "telegramcollector")
    db_user = os.getenv("DB_USER", "postgres")
    db_password = os.getenv("DB_PASSWORD", "")
    encryption_key = os.getenv("CONFIG_STORE_ENCRYPTION_KEY") or db_password

    if not db_password:
        return {}

    try:
        with _psycopg.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password,
            connect_timeout=1,
            autocommit=True,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('collector.config_settings') AS rel")
                rel_row = cur.fetchone()
                if not rel_row or rel_row[0] is None:
                    return {}

                cur.execute(
                    """
                    SELECT
                        config_key,
                        CASE
                            WHEN is_sensitive THEN pgp_sym_decrypt(value_encrypted, %s)::text
                            ELSE value_plain
                        END AS effective_value
                    FROM collector.config_settings
                    """,
                    (encryption_key,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        _config_logger.debug(
            "DB config snapshot unavailable at startup (%s)",
            type(exc).__name__,
        )
        return {}

    snapshot: Dict[str, str] = {}
    for key, value in rows:
        if value is None:
            continue
        key_text = str(key)
        if key_text in _BOOTSTRAP_ENV_KEYS:
            continue
        snapshot[key_text] = str(value)
    return snapshot


def apply_db_config_snapshot() -> int:
    """Applies DB snapshot values into process env prior to Settings() init."""
    snapshot = load_db_config_snapshot()
    for key, value in snapshot.items():
        os.environ[key] = value
    if snapshot:
        _config_logger.info(
            "Loaded %d runtime settings from config store snapshot",
            len(snapshot),
        )
    return len(snapshot)

class Settings(BaseSettings):
    # Telegram API
    TG_API_ID: int
    TG_API_HASH: str
    BOT_TOKEN: str = ""  # Optional if BOT_TOKENS is set
    BOT_TOKENS: str = ""  # Semicolon-separated name:token pairs
    
    # Hub Configuration
    HUB_GROUP_ID: str | int  # Accepts numeric ID or @username
    
    # Database (Postgres)
    DB_HOST: str = "postgres"
    DB_PORT: int = 5432
    DB_NAME: str = "telegramcollector"
    DB_USER: str = "postgres"
    DB_PASSWORD: str
    
    # Redis (New)
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None
    
    # Processing
    SIMILARITY_THRESHOLD: float = 0.55
    MIN_QUALITY_THRESHOLD: float = 0.67  # Stricter quality check (was 0.5)
    MAX_MEDIA_SIZE_MB: int = 50
    NUM_WORKERS: int = 6
    WORKER_TASK_TIMEOUT: int = 300
    QUEUE_MAX_SIZE: int = 4000  # Backpressure limit (items) - increases with RAM
    USE_GPU: bool = True
    REALTIME_BATCH_SIZE: int = 10
    REALTIME_BATCH_INTERVAL: float = 1.0
    REALTIME_QUEUE_MAX: int = 1000
    
    # Operational
    RUN_MODE: str = "both"  # backfill, realtime, both
    HEALTH_CHECK_INTERVAL: int = 300
    LOGIN_BOT_ID: Optional[str] = None
    SESSIONS_DIR: str = "sessions"  # Directory for session files
    
    # Story Scanning
    STORY_SCAN_INTERVAL: int = 300      # 5 minutes (stories expire in 24h)
    STORY_SCAN_ENABLED: bool = True     # Can disable via env
    STORY_PRIORITY_BOOST: int = 10      # Priority offset for story tasks
    
    # Resilience
    CIRCUIT_BREAKER_THRESHOLD: int = 5  # Failures before opening circuit
    CIRCUIT_BREAKER_TIMEOUT: int = 60   # Seconds before retry (OPEN -> HALF_OPEN)
    MAX_RETRY_ATTEMPTS: int = 3
    RETRY_BASE_DELAY: float = 1.0
    
    # Hub Notifications
    HUB_NOTIFY_BATCH_INTERVAL: int = 30  # Seconds between batched notifications
    HUB_NOTIFY_RATE_LIMIT: int = 100     # Max messages per minute
    NOTIFY_ON_NEW_IDENTITY: bool = True
    NOTIFY_ON_SCAN_MILESTONE: bool = True
    NOTIFY_MILESTONE_INTERVAL: int = 500  # Messages between milestones
    
    # Cleanup Configuration
    CLEANUP_INTERVAL: int = 3600             # 1 hour
    GENERAL_TOPIC_RETENTION_HOURS: int = 12   # Auto-delete after 12h
    
    # Observability
    ENABLE_PROMETHEUS: bool = True
    PROMETHEUS_PORT: int = 8000
    LOG_FORMAT: str = "json"  # "json" or "text"
    
    # Account Scheduling (for sharing accounts across projects)
    ACCOUNT_SCHEDULE_ENABLED: bool = False  # Enable time-based account scheduling
    ACCOUNT_ACTIVE_START: str = "00:00"     # UTC time to activate accounts (HH:MM)
    ACCOUNT_ACTIVE_END: str = "24:00"       # UTC time to deactivate accounts (HH:MM)
    
    # Collector — Phase 3 settings
    COLLECTOR_BACKFILL_MSG_PER_SEC: float = 20.0
    COLLECTOR_BACKFILL_CHAT_DELAY: float = 2.0
    COLLECTOR_MEMBER_FETCH_DELAY: float = 0.5
    COLLECTOR_MEMBER_FETCH_MAX_PER_HOUR: int = 200
    MEDIA_STORE_PATH: str = "/mnt/hdd/media"
    SESSIONS_BASE_PATH: str = "/data/sessions"
    COLLECTOR_MAX_MEDIA_SIZE_MB: int = 50
    COLLECTOR_MEDIA_WORKER_COUNT: int = 4
    COLLECTOR_GROUP_MANAGER_POLL_INTERVAL: int = 60
    
    # Collector — Backfill & Story Settings
    COLLECTOR_BACKFILL_ENABLED: bool = True
    COLLECTOR_BACKFILL_BATCH_SIZE: int = 100
    COLLECTOR_BACKFILL_POLL_INTERVAL: int = 30
    COLLECTOR_ADMIN_LOG_POLL_INTERVAL: int = 300
    COLLECTOR_STORY_SCAN_INTERVAL: int = 600
    COLLECTOR_STORY_EXPIRY_BUFFER: int = 60

    # Production Resilience
    SIGTERM_DRAIN_TIMEOUT: int = 30
    # Seconds to wait for in-flight workers to finish before forced cancellation on SIGTERM.

    REDIS_RECONNECT_INTERVAL: int = 30
    # Seconds between Redis reconnect attempts when redis_available=False.

    REDIS_RECONNECT_MAX_ATTEMPTS: int = 0
    # Maximum reconnect attempts (0 = retry indefinitely).

    SESSION_ROTATION_ENABLED: bool = True
    # When True, _handle_invalid_session() will query for the next active account
    # and fire rotation callbacks so scanning continues on a different session.
    # Set to False to disable automatic session rotation.

    DASHBOARD_PORT_SEARCH_RANGE: int = 20
    # Number of ports above the primary dashboard port to try before giving up.
    # e.g. primary=8501, range=20 → tries 8501–8521.

    # Autoscaler
    MAX_WORKERS: int = 10
    # Maximum number of worker tasks the autoscaler may spawn.

    SCALE_UP_SUSTAINED_SECONDS: int = 60
    # Seconds queue depth must stay above high_watermark before scaling up.

    SCALE_DOWN_SUSTAINED_SECONDS: int = 120
    # Seconds queue depth must stay below low_watermark before scaling down.

    AUTOSCALER_POLL_INTERVAL: int = 15
    # Seconds between autoscaler checks.

    STARTUP_PROBE_MAX_ATTEMPTS: int = 30
    # Total number of probe attempts before giving up and raising RuntimeError.

    STARTUP_PROBE_RETRY_INTERVAL: float = 2.0
    # Seconds to wait between startup probe attempts.

    # Telegram Client Feature Flags (Production-safe rollout)
    ENABLE_MTPROTO_RESET: bool = False  # Reset MTProto state on connect (Phase 1: disabled)
    ENABLE_SESSION_LOCK: bool = True    # Prevent concurrent session access
    MTPROTO_RESET_NEW_SESSIONS_ONLY: bool = True  # Only reset new sessions, not legacy ones

    # Face Recognition Service — Phase 6
    FACE_BOT_TOKENS: str = ""
    # Required. Semicolon-separated name:token pairs for hub publishing bots.
    # Format: "BotName1:123456:ABC;BotName2:789012:DEF"

    FACE_SIMILARITY_THRESHOLD: float = 0.55
    # Cosine similarity threshold for identity matching.

    FACE_MIN_QUALITY_THRESHOLD: float = 0.67
    # Minimum InsightFace detection confidence score (det_score).

    FACE_BATCH_SIZE: int = 10
    # Number of raw_messages rows to process per loop iteration.

    FACE_PROCESSING_ENABLED: bool = False
    # When False, the service starts paused.

    FACE_VIDEO_MAX_FRAMES: int = 10
    # Maximum frames to extract from a video message.

    FACE_CIRCLE_VIDEO_FPS: float = 2.0
    # Frames per second to extract from a circle_video message.

    FACE_POLL_INTERVAL: int = 5
    # Sleep duration in seconds when the batch is empty.

    # User Intelligence Service — configuration
    USER_INTEL_BATCH_SIZE: int = 100
    # Maximum number of user_sightings rows to process per loop iteration.

    USER_INTEL_POLL_INTERVAL: int = 5
    # Sleep duration in seconds when the sightings batch is empty.

    USER_INTEL_PROCESSING_ENABLED: bool = True
    # When False, the service starts in a paused state.

    USER_INTEL_NETWORK_ENABLED: bool = True
    # When False, the NetworkBuilder is skipped entirely.

    # Link Discovery Service — Phase 8 settings
    LINK_DISCOVERY_BATCH_SIZE: int = 100
    # Maximum number of raw_messages rows to fetch per cursor iteration.

    LINK_DISCOVERY_POLL_INTERVAL: int = 5
    # Sleep duration in seconds when the batch returns zero rows.

    LINK_DISCOVERY_PROCESSING_ENABLED: bool = True
    # When False, the cursor loop is paused and no rows are read or written.

    LINK_DISCOVERY_RESOLVE_METADATA: bool = False
    # When False (default), no Telegram API calls are made for metadata resolution.

    LINK_DISCOVERY_RESOLVE_RATE_LIMIT: int = 10
    # Maximum number of Telegram API metadata resolution calls permitted per minute.

    # Bulk Sender Service — Phase 9
    BULK_SENDER_SEND_DELAY: float = 1.5
    # Inter-send delay in seconds. Hard minimum of 1.0 is enforced at runtime;
    # values below 1.0 are clamped to 1.0 and a warning is logged at startup.

    BULK_SENDER_BOT_TOKENS: str = ""
    # Optional semicolon-separated list of bot tokens for sending.
    # When set, bot tokens are used instead of user account sessions.
    # Format: "token1;token2;token3"

    BULK_SENDER_MAX_RETRIES: int = 3
    # Maximum number of retry attempts for a transient Telegram error
    # before the file is skipped and the job continues.

    BULK_SENDER_SESSIONS_PATH: str = "/data/sessions/bulk_sender"
    # Directory containing .session files for user-account-based sending.
    # Session file name format: {phone_number}.session

    # Compatibility aliases
    @property
    def TELEGRAM_API_ID(self) -> int:
        return self.TG_API_ID
    
    @property
    def TELEGRAM_API_HASH(self) -> str:
        return self.TG_API_HASH
    
    @property
    def parsed_bot_tokens(self) -> List[Dict[str, str]]:
        """Parses BOT_TOKENS into a list of {'name': ..., 'token': ...} dicts.
        Falls back to BOT_TOKEN if BOT_TOKENS is empty."""
        if self.BOT_TOKENS and self.BOT_TOKENS.strip():
            result = []
            for entry in self.BOT_TOKENS.split(';'):
                entry = entry.strip()
                if not entry:
                    continue
                # Format: BotName:bot_id:bot_secret (name:token where token contains a colon)
                parts = entry.split(':', 1)
                if len(parts) == 2:
                    result.append({'name': parts[0].strip(), 'token': parts[1].strip()})
                else:
                    # Just a raw token without name
                    result.append({'name': f'bot_{len(result)+1}', 'token': entry})
            if result:
                return result
        # Fallback to single BOT_TOKEN
        return [{'name': 'default', 'token': self.BOT_TOKEN}]

    @property
    def parsed_face_bot_tokens(self) -> List[Dict[str, str]]:
        """Parses FACE_BOT_TOKENS into a list of {'name': ..., 'token': ...} dicts."""
        if self.FACE_BOT_TOKENS and self.FACE_BOT_TOKENS.strip():
            result = []
            for entry in self.FACE_BOT_TOKENS.split(';'):
                entry = entry.strip()
                if not entry:
                    continue
                # Format: BotName:bot_id:bot_secret (name:token where token contains a colon)
                parts = entry.split(':', 1)
                if len(parts) == 2:
                    result.append({'name': parts[0].strip(), 'token': parts[1].strip()})
                else:
                    result.append({'name': f'bot_{len(result)+1}', 'token': entry})
            if result:
                return result
        return []

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"  # Ignore extra env vars (like DOTENV_KEY)
    )

# Global settings instance
try:
    apply_db_config_snapshot()
    settings = Settings()
except Exception as e:
    import sys
    print("\n" + "="*60)
    print("CRITICAL CONFIGURATION ERROR")
    print("="*60)
    print(f"Error details: {e}")
    # ... error handling ...
    sys.exit(1)

# Global Redis client for dynamic settings
_redis_config_client = None

def _get_redis_client():
    """Returns a cached Redis client for config lookups."""
    global _redis_config_client
    if _redis_config_client is None:
        try:
            import redis
            _redis_config_client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                password=settings.REDIS_PASSWORD,
                decode_responses=True,
                socket_timeout=1,
                socket_connect_timeout=1
            )
        except ImportError:
            import logging
            logging.getLogger(__name__).warning(
                "redis package not installed. Dynamic settings disabled. "
                "Install redis-py to enable dynamic configuration."
            )
            _redis_config_client = False  # Mark as unavailable
    return _redis_config_client if _redis_config_client is not False else None

def get_dynamic_setting(key: str, default: Any = None) -> Any:
    """
    Fetches a setting from Redis (dynamic) or falls back to env/default.
    Uses a cached Redis connection to check for overrides.
    """
    # 1. Check Redis for override
    try:
        redis_client = _get_redis_client()
        if redis_client is not None:
            redis_key = f"config:{key}"
            value = redis_client.get(redis_key)
            if value is not None:
                # Attempt type conversion if default is provided
                if default is not None:
                    if isinstance(default, bool):
                        return str(value).lower() == 'true'
                    if isinstance(default, int):
                        return int(value)
                    if isinstance(default, float):
                        return float(value)
                return value
    except Exception:
        pass  # Fallback to static setting
        
    # 2. Return Static/Env Value
    return getattr(settings, key, default)

def set_dynamic_setting(key: str, value: Any):
    """Sets a dynamic setting in Redis."""
    try:
        redis_client = _get_redis_client()
        if redis_client is not None:
            redis_key = f"config:{key}"
            redis_client.set(redis_key, str(value))
            return True
    except Exception:
        return False
    return False


# ============================================
# Hub Group ID Resolution (username → int)
# ============================================
_resolved_hub_id: Optional[int] = None

def get_hub_group_id() -> Optional[int]:
    """Returns the resolved numeric Hub Group ID.
    If HUB_GROUP_ID is numeric, parses it directly.
    If it's a username, returns the cached resolved value (or None if not yet resolved).
    """
    global _resolved_hub_id
    if _resolved_hub_id is not None:
        return _resolved_hub_id
    # Try parsing as int directly
    try:
        _resolved_hub_id = int(settings.HUB_GROUP_ID)
        return _resolved_hub_id
    except (ValueError, TypeError):
        return None  # Username — needs async resolution

async def resolve_hub_group_id(client) -> int:
    """Resolves HUB_GROUP_ID (username or numeric) to an integer ID using a Telegram client.
    Caches the result for all subsequent calls.
    """
    global _resolved_hub_id
    if _resolved_hub_id is not None:
        return _resolved_hub_id
    
    raw = settings.HUB_GROUP_ID
    # Try numeric first
    try:
        _resolved_hub_id = int(raw)
        _config_logger.info(f"Hub Group ID is numeric: {_resolved_hub_id}")
        return _resolved_hub_id
    except (ValueError, TypeError):
        pass
    
    username = raw.lstrip('@')
    _config_logger.info(f"Resolving Hub Group username: @{username}")
    try:
        entity = await client.get_entity(username)
        from telethon.utils import get_peer_id
        _resolved_hub_id = get_peer_id(entity)
        _config_logger.info(f"Resolved @{username} → {_resolved_hub_id}")
        return _resolved_hub_id
    except Exception as e:
        _config_logger.error(f"Failed to resolve Hub Group '@{username}': {e}")
        raise ValueError(f"Cannot resolve Hub Group '@{username}': {e}")
