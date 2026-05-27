"""
Centralized paths for mutable application state.

All runtime-managed files live under the ignored ``data/`` directory.
"""

from __future__ import annotations

from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent  # Go up to project root
DATA_DIR = TOOLKIT_ROOT / "data"
DEFAULT_DOWNLOADS_DIR = DATA_DIR / "downloads"

CONFIG_FILE = DATA_DIR / "config.json"
CLIENT_SECRET_FILE = DATA_DIR / "client_secret.json"
OAUTH_CREDENTIALS_FILE = DATA_DIR / "oauth_credentials.pickle"
TOKEN_FILE = DATA_DIR / "token.json"
SUBSCRIPTIONS_FILE = DATA_DIR / "subscriptions.json"
TARGET_CHANNELS_FILE = DATA_DIR / "target_channels.txt"
DATABASE_FILE = DATA_DIR / "youtube_data.db"
DATABASE_BACKUP_FILE = DATA_DIR / "youtube_data.db.json"
SCRAPED_LINKS_FILE = DATA_DIR / "all_scraped_links.txt"

# Legacy compatibility
APP_DATA_DIR = DATA_DIR

def ensure_app_data_dir() -> Path:
    """Ensure data directory exists. Legacy name kept for compatibility."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


ensure_app_data_dir()
