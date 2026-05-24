import os
import json
import importlib
import sys
from typing import List, Dict, Any, Optional
from pathlib import Path

os.environ['PYTHONIOENCODING'] = 'utf-8'
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def _safe_print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode('ascii', errors='replace').decode('ascii'), flush=True)


class DynamicConfig:
    _instance = None
    _config_cache = None
    _last_modified = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DynamicConfig, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        self.config_file = "src/core/config.py"
        self.config_path = Path(self.config_file)

    def get_accounts(self) -> List[Dict[str, Any]]:
        try:
            current_modified = self.config_path.stat().st_mtime if self.config_path.exists() else 0

            if (self._config_cache is None or
                self._last_modified is None or
                current_modified > self._last_modified):
                self._reload_config()
                self._last_modified = current_modified

            return self._config_cache.copy() if self._config_cache else []

        except Exception as e:
            _safe_print(f"Warning: Error loading accounts: {e}")
            return self._get_fallback_accounts()

    def _reload_config(self):
        try:
            if 'src.core.config' in sys.modules:
                importlib.reload(sys.modules['src.core.config'])

            from src.core import config
            self._config_cache = config.ACCOUNTS.copy()

            _safe_print(f"[config] Reloaded: {len(self._config_cache)} accounts")

        except Exception as e:
            _safe_print(f"[config] Failed to reload config: {e}")
            self._config_cache = self._get_fallback_accounts()

    def _get_fallback_accounts(self) -> List[Dict[str, Any]]:
        """Fallback: return empty list and direct user to .env file."""
        _safe_print("[config] Primary config load failed. Ensure .env file is configured.")
        return []

    def force_reload(self):
        self._last_modified = None
        self._config_cache = None
        return self.get_accounts()

    def get_account_count(self) -> int:
        return len(self.get_accounts())

    def get_account_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        accounts = self.get_accounts()
        for account in accounts:
            if account['name'].lower() == name.lower():
                return account
        return None

    def verify_sessions(self) -> Dict[str, bool]:
        accounts = self.get_accounts()
        session_status = {}

        for account in accounts:
            session_file = account['session_file']
            session_status[account['name']] = os.path.exists(session_file)

        return session_status


dynamic_config = DynamicConfig()


def reload_config():
    return dynamic_config.force_reload()


def get_accounts():
    return dynamic_config.get_accounts()


def reload_accounts():
    return dynamic_config.force_reload()


def get_config_value(key, default=None):
    try:
        from src.core import config as _cfg
        return getattr(_cfg, key, default)
    except Exception:
        return default


try:
    from src.core import config
    ACCOUNTS = config.ACCOUNTS
    DATA_DIR = getattr(config, 'DATA_DIR', 'data')
    DOWNLOADS_DIR = getattr(config, 'DOWNLOADS_DIR', 'downloads')
    WEB_DIR = getattr(config, 'WEB_DIR', 'web')
    GROUP_LINK_PATTERN = getattr(config, 'GROUP_LINK_PATTERN', r'(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/|(?:c/)?(?:\w+))')
    MIN_DELAY = getattr(config, 'MIN_DELAY', 4)
    MAX_DELAY = getattr(config, 'MAX_DELAY', 7)
    MAX_RETRIES = getattr(config, 'MAX_RETRIES', 3)
    RETRY_DELAY = getattr(config, 'RETRY_DELAY', 5)
    LINKS_FILE = getattr(config, 'LINKS_FILE', 'data/collected_links.txt')
    JOINED_LINKS_FILE = getattr(config, 'JOINED_LINKS_FILE', 'data/joined_links.txt')
    VALID_LINKS_FILE = getattr(config, 'VALID_LINKS_FILE', 'data/valid_links.txt')
    USERS_CSV = getattr(config, 'USERS_CSV', 'data/Users.csv')
    MEMBERSHIPS_CSV = getattr(config, 'MEMBERSHIPS_CSV', 'data/Memberships.csv')
    PROGRESS_FILE = getattr(config, 'PROGRESS_FILE', 'data/analysis_progress_new.json')
    DOWNLOAD_HASHES_FILE = getattr(config, 'DOWNLOAD_HASHES_FILE', 'data/downloaded_hashes.json')
    
    # Multi-platform link collection constants
    MULTI_PLATFORM_LINKS_FILE = getattr(config, 'MULTI_PLATFORM_LINKS_FILE', 'data/multi_platform_links.txt')
    MULTI_PLATFORM_PATTERNS = {
        'whatsapp': r'(?:https?://)?(?:www\.)?chat\.whatsapp\.com/([A-Za-z0-9]+)',
        'discord': r'(?:https?://)?(?:www\.)?discord\.gg/([A-Za-z0-9]+)',
        'discord_alt': r'(?:https?://)?(?:www\.)?discord\.com/invite/([A-Za-z0-9]+)',
        'facebook': r'(?:https?://)?(?:www\.)?facebook\.com/groups/([A-Za-z0-9_.-]+)',
        'facebook_alt': r'(?:https?://)?(?:m\.)?facebook\.com/groups/([A-Za-z0-9_.-]+)',
        'instagram': r'(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9_.-]+)',
        'twitter': r'(?:https?://)?(?:www\.)?twitter\.com/([A-Za-z0-9_.-]+)',
        'twitter_alt': r'(?:https?://)?(?:www\.)?x\.com/([A-Za-z0-9_.-]+)',
        'reddit': r'(?:https?://)?(?:www\.)?reddit\.com/r/([A-Za-z0-9_-]+)',
        'reddit_alt': r'(?:https?://)?(?:www\.)?redd\.it/([A-Za-z0-9]+)',
    }
except Exception as e:
    _safe_print(f"Warning: Could not load config: {e}")
    ACCOUNTS = []
    DATA_DIR = 'data'
    DOWNLOADS_DIR = 'downloads'
    WEB_DIR = 'web'
    GROUP_LINK_PATTERN = r'(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/|(?:c/)?(?:\w+))'
    MIN_DELAY = 4
    MAX_DELAY = 7
    MAX_RETRIES = 3
    RETRY_DELAY = 5
    LINKS_FILE = 'data/collected_links.txt'
    JOINED_LINKS_FILE = 'data/joined_links.txt'
    VALID_LINKS_FILE = 'data/valid_links.txt'
    USERS_CSV = 'data/Users.csv'
    MEMBERSHIPS_CSV = 'data/Memberships.csv'
    PROGRESS_FILE = 'data/analysis_progress_new.json'
    DOWNLOAD_HASHES_FILE = 'data/downloaded_hashes.json'
    MULTI_PLATFORM_LINKS_FILE = 'data/multi_platform_links.txt'
    MULTI_PLATFORM_PATTERNS = {
        'whatsapp': r'(?:https?://)?(?:www\.)?chat\.whatsapp\.com/([A-Za-z0-9]+)',
        'discord': r'(?:https?://)?(?:www\.)?discord\.gg/([A-Za-z0-9]+)',
        'discord_alt': r'(?:https?://)?(?:www\.)?discord\.com/invite/([A-Za-z0-9]+)',
        'facebook': r'(?:https?://)?(?:www\.)?facebook\.com/groups/([A-Za-z0-9_.-]+)',
        'facebook_alt': r'(?:https?://)?(?:m\.)?facebook\.com/groups/([A-Za-z0-9_.-]+)',
        'instagram': r'(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9_.-]+)',
        'twitter': r'(?:https?://)?(?:www\.)?twitter\.com/([A-Za-z0-9_.-]+)',
        'twitter_alt': r'(?:https?://)?(?:www\.)?x\.com/([A-Za-z0-9_.-]+)',
        'reddit': r'(?:https?://)?(?:www\.)?reddit\.com/r/([A-Za-z0-9_-]+)',
        'reddit_alt': r'(?:https?://)?(?:www\.)?redd\.it/([A-Za-z0-9]+)',
    }
