"""
Unified Instagram Toolkit - Configuration

Credentials are loaded from a .env file in the toolkit root directory.
See .env.example for the expected format.
"""
import os
import sys
from pathlib import Path

from src.download_path_manager import prompt_for_download_path, get_session_path

def get_downloads_directory() -> str:
    """
    Get downloads directory using unified path manager.
    Returns the validated directory path.
    
    Default: ./downloads (relative to toolkit root)
    """
    # Check if path already set in current session
    session_path = get_session_path()
    if session_path:
        return session_path
    
    # Set default path relative to toolkit root
    toolkit_root = Path(__file__).resolve().parents[1]
    default_path = str(toolkit_root / "downloads")
    
    # Prompt for new path with default
    downloads_dir = prompt_for_download_path(
        context="Instagram media",
        out_path=None,
        default_path=default_path
    )
    
    return downloads_dir


# --------------- Credential Loading ---------------

def _load_accounts_from_env() -> list:
    """Load Instagram accounts from .env file.

    Expected keys per account (N = 1, 2, 3, ...):
        INSTA_ACCOUNT_{N}_NAME
        INSTA_ACCOUNT_{N}_USER
        INSTA_ACCOUNT_{N}_PASS
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        print("[WARNING] python-dotenv not installed. Run: pip install python-dotenv")
        print("[WARNING] Falling back to empty account list.")
        return []

    # .env lives in the project root (one level up from src/)
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        print(f"[ERROR] .env file not found at {env_path}")
        print("[ERROR] Create a .env file with your Instagram credentials. See .gitignore for format.")
        return []

    env = dotenv_values(env_path)
    accounts = []
    n = 1
    while True:
        name = env.get(f"INSTA_ACCOUNT_{n}_NAME")
        user = env.get(f"INSTA_ACCOUNT_{n}_USER")
        pw = env.get(f"INSTA_ACCOUNT_{n}_PASS")
        browser = env.get(f"INSTA_ACCOUNT_{n}_BROWSER")
        
        if not name and not user:
            break
        if name and user and pw:
            account_data = {"name": name, "username": user, "password": pw}
            if browser:
                account_data["browser"] = browser.strip()
            accounts.append(account_data)
        else:
            print(f"[WARNING] Incomplete credentials for account {n} — skipping")
        n += 1

    if not accounts:
        print("[WARNING] No valid accounts found in .env file")
    return accounts


# Instagram accounts configuration (loaded from .env)
# The first account in the list is used as the default for batch processing.
INSTAGRAM_ACCOUNTS = _load_accounts_from_env()

# Directory configuration
DATA_DIR = "data"
SESSIONS_DIR = "sessions"
ARCHIVED_LOGS_DIR = "archived_logs"

# Data files
USERNAMES_FILE = f"{DATA_DIR}/usernames.txt"
RELATIONSHIPS_FILE = f"{DATA_DIR}/relationships.json"

# Progress tracking files (legacy JSON files - database is now primary)
SPIDER_PROGRESS_FILE = f"{DATA_DIR}/spider_progress.json"
DOWNLOAD_PROGRESS_FILE = f"{DATA_DIR}/download_progress.json"
BATCH_STATE_FILE = f"{DATA_DIR}/batch_state.json"

# --------------- Rate Limiting & Anti-Ban ---------------
MIN_DELAY = 20                 # Increased to ensure <180 req/hr (was 3)
MAX_DELAY = 40                 # Wider range for more human-like patterns (was 8)

# ADD: Random micro-delays for human-like behavior
MIN_RANDOM_DELAY = 0.3        # Small random delay (0.3-1.0s)
MAX_RANDOM_DELAY = 1.0

# ADD: Human rest periods (occasional longer breaks)
HUMAN_REST_INTERVAL = 40       # Every N ops, chance for rest
HUMAN_REST_CHANCE = 0.3        # 30% chance to rest when interval reached
HUMAN_REST_MIN = 30           # Minimum rest (30-60s)
HUMAN_REST_MAX = 60

# Periodic pause during follower/following enumeration (with jitter)
ENUM_PAUSE_EVERY = 12          # Reduced from 15 to trigger more frequent pauses
ENUM_PAUSE_MIN = 25            # PHASE 1: Random 25-45s instead of fixed 30s
ENUM_PAUSE_MAX = 45            # PHASE 1: Adds unpredictability
ENUM_PAUSE_SECONDS = 30        # Fixed pause duration (kept for backward compatibility)

# Per-item sleep inside enumeration loops — prevents machine-speed API hits
# Real humans scroll slowly; these add 0.5–2s between each follower/followee fetched
ENUM_ITEM_SLEEP_MIN = 0.5
ENUM_ITEM_SLEEP_MAX = 2.0

# Batch size limits (PHASE 1: Prevent long sessions)
MAX_FOLLOWERS_PER_SESSION = 100   # Stop after 100 followers, resume later
MAX_FOLLOWING_PER_SESSION = 100   # Stop after 100 following, resume later
SESSION_COOLDOWN_HOURS = 2        # Wait 2 hours before resuming same account

# Automatic long break thresholds (randomised within range)
OPS_BEFORE_BREAK_MIN = 30      # Increased to mimic sustained human activity (was 5)
OPS_BEFORE_BREAK_MAX = 50      # Increased to mimic sustained human activity (was 15)
BREAK_DURATION_MIN = 5         # minutes - Increased for longer rest periods (was 3)
BREAK_DURATION_MAX = 10        # minutes - Increased for longer rest periods (was 8)

# Emergency break on severe rate-limit (minutes, randomised within range)
EMERGENCY_BREAK_MIN = 5
EMERGENCY_BREAK_MAX = 10

# Account switch delay (seconds, randomised within range) - PHASE 1: Increased significantly
ACCOUNT_SWITCH_DELAY_MIN = 180  # 3 minutes (was 60) - gives Instagram time to "forget"
ACCOUNT_SWITCH_DELAY_MAX = 300  # 5 minutes (was 120) - more human-like switching

# Warm-up operations (PHASE 2: Light activity before heavy operations)
WARMUP_ENABLED = True              # Enable warm-up period before bulk operations
WARMUP_OPERATIONS = 3              # Number of light operations (view profile, check feed, etc.)
WARMUP_DELAY_MIN = 30              # 30-60s between warm-up operations
WARMUP_DELAY_MAX = 60

# Session management (PHASE 2: Refresh sessions periodically)
SESSION_MAX_AGE_DAYS = 7           # Refresh sessions older than 7 days
SESSION_REFRESH_ON_RATE_LIMIT = True  # Create new session after rate limit

# Smart scheduling (PHASE 3: Avoid peak detection times)
SMART_SCHEDULING_ENABLED = True
# Night hours (11pm–7am): real humans sleep — multiply delay by 2.5–4x (drawn fresh each hour)
NIGHT_HOURS = [23, 0, 1, 2, 3, 4, 5, 6]
NIGHT_DELAY_MULTIPLIER_MIN = 2.5
NIGHT_DELAY_MULTIPLIER_MAX = 4.0
# Business hours: higher detection risk — multiply delay by 1.5x
SAFE_HOURS = [7, 8, 20, 21, 22]  # Early morning / evening — normal
RISKY_HOURS = [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]
RISKY_HOUR_DELAY_MULTIPLIER = 1.5

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 30.0        # seconds
RETRY_MAX_DELAY = 600.0        # seconds
RATE_LIMIT_FLOOR = 300.0       # minimum delay on rate-limit (seconds)

# Download pacing
DOWNLOAD_PAUSE_EVERY = 10      # pause every N posts downloaded
DOWNLOAD_PAUSE_SECONDS = 10    # seconds to pause

# Per-account cooldown after rate-limit hit (minutes)
ACCOUNT_COOLDOWN_MINUTES = 15

# Daily quota budget per account (0 = unlimited)
DAILY_QUOTA_PROFILE_VIEWS = 180    # Instagram ~200/hr, stay under
DAILY_QUOTA_ACTIONS = 6000         # Instagram ~7500/day, stay under
QUOTA_RESET_HOUR = 0               # hour of day (0-23) to reset quotas

# --------------- Human-Like Randomization (More Human Rate Limiting Fix) ---------------

# Micro-pause configuration: short thinking/reading pauses between operations
MICRO_PAUSE_MIN = 0.5          # Minimum micro-pause duration (seconds)
MICRO_PAUSE_MAX = 3.0          # Maximum micro-pause duration (seconds)
MICRO_PAUSE_PROBABILITY = 0.7  # Probability of micro-pause per operation (70%)

# Distribution mix: weights for selecting delay distribution type
# Gaussian (bell curve) for most delays, Uniform for variety, Exponential for occasional long pauses
DISTRIBUTION_GAUSSIAN_WEIGHT = 0.6   # 60% of delays use Gaussian distribution
DISTRIBUTION_UNIFORM_WEIGHT = 0.3    # 30% of delays use Uniform distribution
DISTRIBUTION_EXPONENTIAL_WEIGHT = 0.1  # 10% of delays use Exponential distribution

# Variable enumeration pause configuration (replaces fixed ENUM_PAUSE_EVERY=12)
ENUM_PAUSE_INTERVAL_MIN = 10   # Minimum items between enumeration pauses
ENUM_PAUSE_INTERVAL_MAX = 15   # Maximum items between enumeration pauses
ENUM_PAUSE_DURATION_MIN = 20   # Minimum enumeration pause duration (seconds)
ENUM_PAUSE_DURATION_MAX = 60   # Maximum enumeration pause duration (seconds)

# Content-aware delay configuration
CONTENT_AWARE_ENABLED = True         # Enable content-aware delay adjustments
CONTENT_AWARE_MAX_MULTIPLIER = 2.0   # Maximum multiplier for content-aware delays (2x)

# Variable delay range configuration: adds ±10% variation to min/max bounds per call
DELAY_RANGE_VARIATION = 0.1    # ±10% variation applied to delay range per call

# --------------- Proxy (optional) ---------------
# Set in .env as PROXY_URL=socks5://user:pass@host:port
# Or per-account: INSTA_ACCOUNT_1_PROXY=socks5://...
def _load_proxy_config() -> dict:
    """Load proxy configuration from .env."""
    try:
        from dotenv import dotenv_values
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if not os.path.exists(env_path):
            return {}
        env = dotenv_values(env_path)
        proxies = {}
        # Global proxy
        global_proxy = env.get('PROXY_URL', '').strip()
        if global_proxy:
            proxies['__global__'] = global_proxy
        # Per-account proxy
        n = 1
        while True:
            name = env.get(f'INSTA_ACCOUNT_{n}_NAME')
            if not name:
                break
            proxy = env.get(f'INSTA_ACCOUNT_{n}_PROXY', '').strip()
            if proxy:
                proxies[name] = proxy
            n += 1
        return proxies
    except Exception:
        return {}

PROXY_CONFIG = _load_proxy_config()

# --------------- Download Filters ---------------
def _load_filter_config() -> dict:
    """Load download filter settings from .env."""
    try:
        from dotenv import dotenv_values
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        env = dotenv_values(env_path) if os.path.exists(env_path) else {}
        raw = env.get('FILTER_MAX_FOLLOWERS', '0').strip()
        return {'max_followers': int(raw) if raw.isdigit() else 0}
    except Exception:
        return {'max_followers': 0}

_filter_cfg = _load_filter_config()
# Max followers allowed for downloads. 0 = no filter (download everyone).
FILTER_MAX_FOLLOWERS: int = _filter_cfg['max_followers']

# --------------- Error Detection Phrases ---------------
# Phrases that indicate rate limiting / temporary blocks
RATE_LIMIT_PHRASES = (
    "please wait a few minutes",
    "rate limit",
    "too many requests",
    "temporarily blocked",
    "401 unauthorized",
    "try again later",
)

# Phrases that indicate the account needs manual intervention
CHALLENGE_PHRASES = (
    "checkpoint_required",
    "challenge_required",
    "consent_required",
    "feedback_required",
    "login_required",
    "suspicious activity",
    "account has been disabled",
    "your account has been temporarily locked",
)

# Phrases that trigger account switching (not just backoff)
ACCOUNT_SWITCH_PHRASES = (
    "bad credentials",
    "2fa",
    "two factor",
    "verification code",
    "authentication",
    "login_required",
)

# --------------- Operation Registry ---------------
# Maps operation names to their metadata for the OperationClassifier.
# Each entry must have: operation_type, rate_limit_weight (1-10), description.
OPERATION_REGISTRY = {
    "download_profile_pic": {
        "operation_type": "PUBLIC",
        "rate_limit_weight": 2,
        "description": "Download a user's profile picture (public access)",
    },
    "get_basic_info": {
        "operation_type": "PUBLIC",
        "rate_limit_weight": 1,
        "description": "Retrieve basic profile information (public access)",
    },
    "download_stories": {
        "operation_type": "FOLLOWING_REQUIRED",
        "rate_limit_weight": 7,
        "description": "Download a user's stories (requires following)",
    },
    "download_highlights": {
        "operation_type": "FOLLOWING_REQUIRED",
        "rate_limit_weight": 6,
        "description": "Download a user's story highlights (requires following)",
    },
    "download_media": {
        "operation_type": "FOLLOWING_REQUIRED",
        "rate_limit_weight": 5,
        "description": "Download a user's media posts (requires following)",
    },
    "get_followers": {
        "operation_type": "PUBLIC",
        "rate_limit_weight": 8,
        "description": "Retrieve a user's followers list (public access)",
    },
    "get_following": {
        "operation_type": "PUBLIC",
        "rate_limit_weight": 8,
        "description": "Retrieve a user's following list (public access)",
    },
}


def get_default_account() -> dict | None:
    """
    Get the default account for batch processing.
    Returns the first account in INSTAGRAM_ACCOUNTS list.
    """
    if INSTAGRAM_ACCOUNTS:
        return INSTAGRAM_ACCOUNTS[0]
    return None

def get_account_by_name(account_name: str | None) -> dict | None:
    """
    Get account configuration by name.
    Returns None if account not found.
    """
    if not account_name:
        return get_default_account()
    
    return next((a for a in INSTAGRAM_ACCOUNTS if a['name'] == account_name), None)


# --------------- Sliding Window Rate Limiting Configuration ---------------

# Enable/disable sliding window rate limiting (default: disabled for backward compatibility)
# Set via environment variable or .env file
SLIDING_WINDOW_ENABLED = os.getenv('SLIDING_WINDOW_ENABLED', 'false').lower() == 'true'

# Default sliding window limits (can be overridden per-account in database)
# These are maximum requests per time window
DEFAULT_WINDOW_1H_LIMIT = int(os.getenv('WINDOW_1H_LIMIT', '180'))      # 180 requests/hour
DEFAULT_WINDOW_3H_LIMIT = int(os.getenv('WINDOW_3H_LIMIT', '400'))     # 400 requests/3hours
DEFAULT_WINDOW_5H_LIMIT = int(os.getenv('WINDOW_5H_LIMIT', '600'))     # 600 requests/5hours
DEFAULT_WINDOW_1D_LIMIT = int(os.getenv('WINDOW_1D_LIMIT', '2000'))    # 2000 requests/day

# Request log retention period (old logs are cleaned up)
REQUEST_LOG_RETENTION_HOURS = int(os.getenv('REQUEST_LOG_RETENTION_HOURS', '24'))

# Optional: Machine ID for debugging/monitoring (auto-detected if not set)
MACHINE_ID = os.getenv('MACHINE_ID', None)

