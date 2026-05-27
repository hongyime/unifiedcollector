# Unified Telegram Toolkit Configuration
# Credentials are loaded from .env file (falls back to hardcoded values)
import os
from pathlib import Path

def _load_env():
    """Load .env file if python-dotenv is available, otherwise parse manually."""
    env_path = Path(__file__).resolve().parent.parent.parent / '.env'
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except ImportError:
        # Manual .env parser (no dependency needed)
        if env_path.exists():
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, _, value = line.partition('=')
                        os.environ.setdefault(key.strip(), value.strip())

_load_env()

def _build_accounts():
    """Build ACCOUNTS list from environment variables."""
    accounts = []
    for i in range(1, 20):  # Support up to 20 accounts
        name = os.environ.get(f'ACCOUNT_{i}_NAME')
        if not name:
            break
        accounts.append({
            "name": name,
            "api_id": int(os.environ.get(f'ACCOUNT_{i}_API_ID', '0')),
            "api_hash": os.environ.get(f'ACCOUNT_{i}_API_HASH', ''),
            "phone": os.environ.get(f'ACCOUNT_{i}_PHONE', ''),
            "session_file": os.environ.get(f'ACCOUNT_{i}_SESSION', f'sessions/{name}.session'),
            "prefix": os.environ.get(f'ACCOUNT_{i}_PREFIX', name[:4]),
        })
    return accounts

ACCOUNTS = _build_accounts()

# Fallback if .env is missing/empty
if not ACCOUNTS:
    print("WARNING: No accounts configured. Create a .env file with account credentials.")
    print("         See .env.example for the required format.")

# Backup/Resender default group
BACKUP_GROUP_ID = int(os.environ.get('BACKUP_GROUP_ID', '-1001522260324'))

# File paths
DATA_DIR = "data"
DOWNLOADS_DIR = "downloads"
WEB_DIR = "web"

# Link collection settings
GROUP_LINK_PATTERN = r'https?://t\.me/(?:joinchat/[\w-]+|\+[\w-]+|[a-zA-Z0-9_]{5,})'

# Joining settings
MIN_DELAY = 4
MAX_DELAY = 7

# Analysis settings
MAX_RETRIES = 3
RETRY_DELAY = 5
ACCOUNT_RECONNECT_MAX_ATTEMPTS = 3
ACCOUNT_RECONNECT_BASE_DELAY = 2.0

# Unified scan performance settings
# These apply to message/history scanning and are intentionally much lower
# than join/leave timing, which benefits from slower pacing.
SCAN_MIN_DELAY = 0.0
SCAN_MAX_DELAY = 0.15
SCAN_DELAY_EVERY_MESSAGES = 25
SCAN_GROUP_DELAY_SECONDS = 0.25

# Shutdown behavior
# When Ctrl+C is pressed, cooperative shutdown starts first.
# Remaining account tasks are force-cancelled after this timeout.
SHUTDOWN_CANCEL_TIMEOUT_SECONDS = 1.5
SHUTDOWN_POLL_INTERVAL_SECONDS = 0.2

# Dialog discovery limits
# Maximum number of dialogs to scan when discovering targets
# Set to 0 for unlimited (not recommended for accounts with thousands of dialogs)
MAX_DIALOGS_LIMIT = int(os.getenv("MAX_DIALOGS_LIMIT", "1000"))

# User analysis enrichment settings
# These control how aggressively the active processor-backed analyzer tries
# to discover users beyond message senders.
USER_ANALYZER_COLLECT_PARTICIPANTS = True
USER_ANALYZER_COLLECT_LINKED_CHAT_PARTICIPANTS = True
USER_ANALYZER_COLLECT_REPLY_SENDERS = True
USER_ANALYZER_COLLECT_MENTIONED_USERS = True
USER_ANALYZER_COLLECT_VIA_BOTS = True
USER_ANALYZER_COLLECT_FORWARDED_USERS = True
USER_ANALYZER_COLLECT_ACTION_USERS = True
USER_ANALYZER_COLLECT_LINKED_CHAT_MESSAGES = True
USER_ANALYZER_COLLECT_ADMIN_LOG = True
USER_ANALYZER_LINKED_CHAT_MESSAGE_LIMIT = 250
USER_ANALYZER_ADMIN_LOG_LIMIT = 200

# Output files
LINKS_FILE = f"{DATA_DIR}/collected_links.txt"
JOINED_LINKS_FILE = f"{DATA_DIR}/joined_links.txt"
VALID_LINKS_FILE = f"{DATA_DIR}/valid_links.txt"
USERS_CSV = f"{DATA_DIR}/Users.csv"
MEMBERSHIPS_CSV = f"{DATA_DIR}/Memberships.csv"
PROGRESS_FILE = f"{DATA_DIR}/scan_progress.json"
JOIN_PROGRESS_FILE = f"{DATA_DIR}/join_progress.json"
ANALYSIS_PROGRESS_FILE = f"{DATA_DIR}/analysis_progress.json"
DOWNLOAD_HASHES_FILE = f"{DATA_DIR}/downloaded_hashes.json"
PROFILE_PHOTOS_FILE = f"{DATA_DIR}/downloaded_profile_photos.json"

# ── SQLite Migration Feature Flags ──
# Control gradual migration from JSON/text files to SQLite
# Set to True to enable SQLite for each component, False to use legacy files

# Use SQLite for scan progress tracking (replaces scan_progress.json, analysis_progress.json)
USE_SQLITE_FOR_SCAN_PROGRESS = True

# Use SQLite for link collection (replaces collected_links.txt)
USE_SQLITE_FOR_LINKS = True

# Use SQLite for download hashes (replaces downloaded_hashes.txt/json)
USE_SQLITE_FOR_HASHES = True

# Use SQLite for photo send progress (replaces photo_send_progress.json)
USE_SQLITE_FOR_PHOTO_SEND_PROGRESS = True

# Use SQLite for profile photo tracking (replaces downloaded_profile_photos.json)
USE_SQLITE_FOR_PROFILE_PHOTOS = True

# Migration phase: 'dual_write' (write to both), 'sqlite_only', or 'rollback'
MIGRATION_PHASE = 'sqlite_only'
