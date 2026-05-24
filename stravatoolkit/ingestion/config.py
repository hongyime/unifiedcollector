from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
FRONTEND_DIST_DIR = BASE_DIR / "frontend" / "dist"
DEFAULT_DOWNLOADS_DIR = BASE_DIR / "downloads"
DEFAULT_DB_PATH = DATA_DIR / "strava_sync.db"
DEFAULT_ENV_PATH = DATA_DIR / ".env"
STRAVA_BASE_URL = "https://www.strava.com"
STRAVA_TIMEZONE = ZoneInfo("Asia/Singapore")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class Settings:
    db_path: Path = DEFAULT_DB_PATH
    env_path: Path = DEFAULT_ENV_PATH
    log_dir: Path = LOG_DIR
    frontend_dist_dir: Path = FRONTEND_DIST_DIR
    downloads_dir: Path = DEFAULT_DOWNLOADS_DIR
    timezone: ZoneInfo = STRAVA_TIMEZONE
    user_agent: str = DEFAULT_USER_AGENT
    backfill_steps: int = 25
    backfill_parallelism: int = 3
    backfill_year_cap: int = 25
    debug_http: bool = False
    debug_delays: bool = False
    rate_limit_retries: int = 2
    rate_limit_backoff_seconds: int = 60
    request_timeout_seconds: int = 20
    auth_recovery_backoff_seconds: int = 30
    auth_recovery_backoff_cap_seconds: int = 300
    # Dynamic delay configuration (min/max seconds)
    # Min delay is 5 seconds to avoid 429 rate limits
    # Max delay for rate limit retries is 300 seconds (5 minutes)
    api_delay_min_seconds: float = 5.0
    api_delay_max_seconds: float = 10.0
    feed_delay_min_seconds: float = 5.0
    feed_delay_max_seconds: float = 12.0
    backfill_delay_min_seconds: float = 5.0
    backfill_delay_max_seconds: float = 15.0
    stream_delay_min_seconds: float = 5.0
    stream_delay_max_seconds: float = 8.0
    roster_delay_min_seconds: float = 5.0
    roster_delay_max_seconds: float = 10.0


def load_settings() -> Settings:
    db_path = Path(os.getenv("DB_PATH", DEFAULT_DB_PATH))
    env_path = Path(os.getenv("ENV_PATH", DEFAULT_ENV_PATH))
    backfill_steps = int(os.getenv("BACKFILL_STEPS", os.getenv("BACKFILL_BUDGET_MINUTES", "25")))
    backfill_parallelism = max(1, int(os.getenv("BACKFILL_PARALLELISM", "3")))
    backfill_year_cap = max(0, int(os.getenv("BACKFILL_YEAR_CAP", "25")))
    debug_http = os.getenv("DEBUG_HTTP", "").strip().lower() in {"1", "true", "yes", "on"}
    debug_delays = os.getenv("DEBUG_DELAYS", "").strip().lower() in {"1", "true", "yes", "on"}
    rate_limit_retries = max(0, int(os.getenv("RATE_LIMIT_RETRIES", "2")))
    rate_limit_backoff_seconds = max(1, int(os.getenv("RATE_LIMIT_BACKOFF_SECONDS", "60")))
    request_timeout_seconds = max(1, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")))
    auth_recovery_backoff_seconds = max(1, int(os.getenv("AUTH_RECOVERY_BACKOFF_SECONDS", "30")))
    auth_recovery_backoff_cap_seconds = max(
        auth_recovery_backoff_seconds,
        int(os.getenv("AUTH_RECOVERY_BACKOFF_CAP_SECONDS", "300")),
    )
    
    # Dynamic delay configuration (min 5 seconds to avoid 429)
    api_delay_min = float(os.getenv("API_DELAY_MIN_SECONDS", "5.0"))
    api_delay_max = float(os.getenv("API_DELAY_MAX_SECONDS", "10.0"))
    feed_delay_min = float(os.getenv("FEED_DELAY_MIN_SECONDS", "5.0"))
    feed_delay_max = float(os.getenv("FEED_DELAY_MAX_SECONDS", "12.0"))
    backfill_delay_min = float(os.getenv("BACKFILL_DELAY_MIN_SECONDS", "5.0"))
    backfill_delay_max = float(os.getenv("BACKFILL_DELAY_MAX_SECONDS", "15.0"))
    stream_delay_min = float(os.getenv("STREAM_DELAY_MIN_SECONDS", "5.0"))
    stream_delay_max = float(os.getenv("STREAM_DELAY_MAX_SECONDS", "8.0"))
    roster_delay_min = float(os.getenv("ROSTER_DELAY_MIN_SECONDS", "5.0"))
    roster_delay_max = float(os.getenv("ROSTER_DELAY_MAX_SECONDS", "10.0"))
    
    # Validate delay ranges
    api_delay_min, api_delay_max = _validate_delay_range(api_delay_min, api_delay_max)
    feed_delay_min, feed_delay_max = _validate_delay_range(feed_delay_min, feed_delay_max)
    backfill_delay_min, backfill_delay_max = _validate_delay_range(backfill_delay_min, backfill_delay_max)
    stream_delay_min, stream_delay_max = _validate_delay_range(stream_delay_min, stream_delay_max)
    roster_delay_min, roster_delay_max = _validate_delay_range(roster_delay_min, roster_delay_max)
    
    return Settings(
        db_path=db_path,
        env_path=env_path,
        log_dir=LOG_DIR,
        frontend_dist_dir=FRONTEND_DIST_DIR,
        downloads_dir=Path(os.getenv("DOWNLOADS_DIR", DEFAULT_DOWNLOADS_DIR)),
        timezone=STRAVA_TIMEZONE,
        user_agent=os.getenv("STRAVA_USER_AGENT", DEFAULT_USER_AGENT),
        backfill_steps=backfill_steps,
        backfill_parallelism=backfill_parallelism,
        backfill_year_cap=backfill_year_cap,
        debug_http=debug_http,
        debug_delays=debug_delays,
        rate_limit_retries=rate_limit_retries,
        rate_limit_backoff_seconds=rate_limit_backoff_seconds,
        request_timeout_seconds=request_timeout_seconds,
        auth_recovery_backoff_seconds=auth_recovery_backoff_seconds,
        auth_recovery_backoff_cap_seconds=auth_recovery_backoff_cap_seconds,
        api_delay_min_seconds=api_delay_min,
        api_delay_max_seconds=api_delay_max,
        feed_delay_min_seconds=feed_delay_min,
        feed_delay_max_seconds=feed_delay_max,
        backfill_delay_min_seconds=backfill_delay_min,
        backfill_delay_max_seconds=backfill_delay_max,
        stream_delay_min_seconds=stream_delay_min,
        stream_delay_max_seconds=stream_delay_max,
        roster_delay_min_seconds=roster_delay_min,
        roster_delay_max_seconds=roster_delay_max,
    )


def _validate_delay_range(min_delay: float, max_delay: float) -> tuple[float, float]:
    """
    Validate and normalize delay range.
    
    Ensures min_delay >= 0, max_delay >= min_delay.
    """
    min_delay = max(0, min_delay)
    max_delay = max(min_delay, max_delay)  # Ensure max >= min
    return min_delay, max_delay


def ensure_runtime_dirs(settings: Settings) -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)
    settings.env_path.parent.mkdir(parents=True, exist_ok=True)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_calendar_date(dt: datetime) -> str:
    return dt.astimezone(STRAVA_TIMEZONE).date().isoformat()


def day_bounds(date_string: str) -> tuple[int, int]:
    year, month, day = [int(part) for part in date_string.split("-")]
    start = datetime(year, month, day, tzinfo=STRAVA_TIMEZONE)
    end = datetime.combine(start.date(), time(23, 59, 59), tzinfo=STRAVA_TIMEZONE)
    return int(start.timestamp()), int(end.timestamp())
