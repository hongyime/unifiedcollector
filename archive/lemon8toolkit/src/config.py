"""
Unified Lemon8 Toolkit - Configuration
"""
import atexit
import os
import sqlite3
import sys
import threading
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

try:
    from path_manager import prompt_for_download_path, get_session_path
    _download_path_manager_available = True
except ImportError:
    def prompt_for_download_path(context=None, out_path=None, default_path=None):
        return str(Path(__file__).parent / "downloads")

    def get_session_path():
        return None

    _download_path_manager_available = False

def get_downloads_directory():
    """
    Get downloads directory using unified path manager.
    Returns the validated directory path.
    """
    # Check if path already set in current session
    session_path = get_session_path()
    if session_path:
        return session_path
    
    # Prompt for an explicit custom path when no session path exists.
    downloads_dir = prompt_for_download_path(
        context="Lemon8 media",
        allow_session_reuse=False,
        out_path=None,
        default_path=None
    )
    
    return downloads_dir

def _get_bool(key: str, default: bool) -> bool:
    """Get boolean value from environment variable."""
    value = os.getenv(key, str(default)).lower()
    return value in ('true', '1', 'yes', 'on')

def _get_int(key: str, default: int) -> int:
    """Get integer value from environment variable."""
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

def _get_float(key: str, default: float) -> float:
    """Get float value from environment variable."""
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default

# Directory configuration
DATA_DIR = os.getenv("DATA_DIR", "data")
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "downloads")

# Rate limiting configuration
MIN_DELAY = _get_float("MIN_DELAY", 1.0)
MAX_DELAY = _get_float("MAX_DELAY", 3.0)
REQUESTS_PER_MINUTE = _get_int("REQUESTS_PER_MINUTE", 30)

# Data files for tracking
VISITED_USERS_FILE = f"{DATA_DIR}/visited_users.json"
PROCESSED_TAGS_FILE = f"{DATA_DIR}/processed_tags.json" 
DOWNLOADED_MEDIA_FILE = f"{DATA_DIR}/downloaded_media.json"
DOWNLOAD_PROGRESS_FILE = f"{DATA_DIR}/download_progress.json"
LEMON8_DB_FILE = f"{DATA_DIR}/lemon8_toolkit.db"

# Scraping configuration
SCRAPE_MODES = {
    'user': 'User Profile Scrape',
    'feed': 'For You Feed Scrape', 
    'tag': 'Tag/Topic Scrape'
}

# Media configuration
SUPPORTED_MEDIA_TYPES = ['mp4', 'jpg', 'jpeg', 'png', 'webp', 'gif']
MAX_MEDIA_SIZE_MB = _get_int("MAX_MEDIA_SIZE_MB", 100)
CHUNK_SIZE = _get_int("CHUNK_SIZE", 8192)

# Image enhancement and verification configuration
IMAGE_ENHANCEMENT_ENABLED = _get_bool("IMAGE_ENHANCEMENT_ENABLED", True)
ENABLE_HIGH_QUALITY_FALLBACK = _get_bool("ENABLE_HIGH_QUALITY_FALLBACK", True)
HIGH_QUALITY_IMAGE_WIDTH = _get_int("HIGH_QUALITY_IMAGE_WIDTH", 2160)
HIGH_QUALITY_IMAGE_HEIGHT = _get_int("HIGH_QUALITY_IMAGE_HEIGHT", 2160)
HIGH_QUALITY_IMAGE_QUALITY = _get_int("HIGH_QUALITY_IMAGE_QUALITY", 100)
MIN_IMAGE_WIDTH = _get_int("MIN_IMAGE_WIDTH", 320)
MIN_IMAGE_HEIGHT = _get_int("MIN_IMAGE_HEIGHT", 320)
MIN_IMAGE_FILE_SIZE_BYTES = _get_int("MIN_IMAGE_FILE_SIZE_BYTES", 8 * 1024)
PROFILE_PHOTO_DOWNLOAD_ENABLED = _get_bool("PROFILE_PHOTO_DOWNLOAD_ENABLED", True)
INCLUDE_PROFILE_IMAGES_IN_USER_SCRAPES = _get_bool("INCLUDE_PROFILE_IMAGES_IN_USER_SCRAPES", True)
INCLUDE_PROFILE_IMAGES_IN_FEED = _get_bool("INCLUDE_PROFILE_IMAGES_IN_FEED", False)
MIN_PROFILE_IMAGE_WIDTH = _get_int("MIN_PROFILE_IMAGE_WIDTH", 100)
MIN_PROFILE_IMAGE_HEIGHT = _get_int("MIN_PROFILE_IMAGE_HEIGHT", 100)
MIN_PROFILE_IMAGE_FILE_SIZE_BYTES = _get_int("MIN_PROFILE_IMAGE_FILE_SIZE_BYTES", 2 * 1024)

# Profile photo blob storage
PROFILE_PHOTO_BLOB_THRESHOLD = _get_int("PROFILE_PHOTO_BLOB_THRESHOLD", 5 * 1024 * 1024 * 1024)  # 5GB

IMAGE_QUALITY_THRESHOLDS = {
    'min_width': MIN_IMAGE_WIDTH,
    'min_height': MIN_IMAGE_HEIGHT,
    'min_file_size_bytes': MIN_IMAGE_FILE_SIZE_BYTES,
    'target_width': HIGH_QUALITY_IMAGE_WIDTH,
    'target_height': HIGH_QUALITY_IMAGE_HEIGHT,
    'target_quality': HIGH_QUALITY_IMAGE_QUALITY,
    'fallback_to_original': ENABLE_HIGH_QUALITY_FALLBACK,
}

# Filename customization configuration
USERNAME_PREFIX_ENABLED = _get_bool("USERNAME_PREFIX_ENABLED", True)
STRICT_USERNAME_VALIDATION = _get_bool("STRICT_USERNAME_VALIDATION", False)
USERNAME_MAX_LENGTH = _get_int("USERNAME_MAX_LENGTH", 64)

USERNAME_PREFIX_CONFIG = {
    'enabled': USERNAME_PREFIX_ENABLED,
    'strict_validation': STRICT_USERNAME_VALIDATION,
    'max_length': USERNAME_MAX_LENGTH,
}

PROFILE_IMAGE_CONFIG = {
    'download_enabled': PROFILE_PHOTO_DOWNLOAD_ENABLED,
    'include_in_user_scrapes': INCLUDE_PROFILE_IMAGES_IN_USER_SCRAPES,
    'include_in_feed': INCLUDE_PROFILE_IMAGES_IN_FEED,
    'min_width': MIN_PROFILE_IMAGE_WIDTH,
    'min_height': MIN_PROFILE_IMAGE_HEIGHT,
    'min_file_size_bytes': MIN_PROFILE_IMAGE_FILE_SIZE_BYTES,
}

# Multi-account configuration
LEMON8_COOKIES_FILE = os.getenv("LEMON8_COOKIES_FILE", "cookies.txt")
LEMON8_ACCOUNT_COOKIES = []
for i in range(1, 10):  # Support up to 9 additional accounts
    account_cookies = os.getenv(f"LEMON8_ACCOUNT_{i}_COOKIES")
    if account_cookies:
        LEMON8_ACCOUNT_COOKIES.append(account_cookies)

# Spidering configuration
SPIDERING_ENABLED = _get_bool("SPIDERING_ENABLED", True)
SPIDER_BATCH_SIZE = _get_int("SPIDER_BATCH_SIZE", 100)
MAX_USER_PROFILE_PAGES = _get_int("MAX_USER_PROFILE_PAGES", 20)  # pages per user (≈12-20 posts each)

# User agent for requests
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Lemon8 URL patterns
LEMON8_BASE_URL = "https://www.lemon8-app.com"
USER_URL_PATTERN = f"{LEMON8_BASE_URL}/@{{}}"  # e.g., @walshdelaney
FEED_URL = f"{LEMON8_BASE_URL}/FEED/FORYOU"
TAG_URL_PATTERN = f"{LEMON8_BASE_URL}/topic/{{}}"  # e.g., topic/7549513626407780359?region=sg
DOWNLOAD_VERIFICATION_LOG_FILE = f"{DATA_DIR}/download_verification.log"

def ensure_data_directory():
    """Ensure data directory exists"""
    os.makedirs(DATA_DIR, exist_ok=True)

def get_user_url(username):
    """Get full URL for a user profile"""
    # Remove @ if present
    username = username.lstrip('@')
    return USER_URL_PATTERN.format(username)

def get_tag_url(tag_id):
    """Get full URL for a tag/topic"""
    return TAG_URL_PATTERN.format(tag_id)

# ── DB connection safety ─────────────────────────────────────────────────────

_db_connections: list = []
_db_conn_lock = threading.Lock()


def configure_db_connection(conn: sqlite3.Connection) -> None:
    """Enable WAL mode + busy timeout; register conn for atexit WAL checkpoint."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    with _db_conn_lock:
        _db_connections.append(conn)


def _db_atexit_handler() -> None:
    with _db_conn_lock:
        for conn in list(_db_connections):
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                conn.close()
            except Exception:
                pass
        _db_connections.clear()


atexit.register(_db_atexit_handler)


def get_media_save_path(downloads_dir, scrape_type, identifier, filename, subfolder_override=None):
    """
    Get the full path where media should be saved
    
    Args:
        downloads_dir: Base downloads directory
        scrape_type: 'user', 'feed', or 'tag'
        identifier: Username, 'foryou', or tag_id 
        filename: Media filename
        subfolder_override: Optional explicit subfolder name (takes precedence)
    """
    if subfolder_override:
        subfolder = str(subfolder_override)
    elif scrape_type == 'user':
        subfolder = f"user_{identifier}"
    elif scrape_type == 'feed':
        subfolder = "foryou_feed"
    elif scrape_type == 'tag':
        subfolder = f"tag_{identifier}"
    else:
        subfolder = "unknown"
    
    media_dir = os.path.join(downloads_dir, subfolder)
    os.makedirs(media_dir, exist_ok=True)
    return os.path.join(media_dir, filename)
