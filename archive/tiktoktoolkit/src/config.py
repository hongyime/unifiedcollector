"""Configuration management for the toolkit."""

from pathlib import Path
from typing import Any, Dict, Optional
import os

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .errors import ConfigError


class AppConfig(BaseModel):
    """Application configuration model."""

    output_root: str = "downloads"
    log_level: str = "INFO"
    cookies_file: Optional[str] = "configs/tiktok_cookies.txt"
    cookies_browser: Optional[str] = None
    tracker_db: str = "data/tiktok_toolkit.db"
    tracker_json_backup: str = "configs/download_tracker.json.backup"
    min_sleep: float = 0.5
    max_sleep: float = 2.0
    retries: int = 2
    timeout_seconds: int = 300
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    providers: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    spider_max_following: int = 500
    spider_max_followers: int = 500


def load_yaml(path: Path) -> Dict[str, Any]:
    """Load YAML configuration file."""
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}")

    with path.open('r', encoding='utf-8') as handle:
        return yaml.safe_load(handle) or {}


def _load_env(base: Path) -> None:
    """Load toolkit root .env file if present."""
    env_path = base / '.env'
    if env_path.exists():
        load_dotenv(env_path, override=False)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _merge_gallerydl_config(app_config: Dict[str, Any], providers_config: Dict[str, Any]) -> Dict[str, Any]:
    merged_providers = dict(providers_config.get('providers', {}) or {})
    gallerydl = dict(merged_providers.get('gallerydl', {}) or {})

    gallerydl.setdefault('cookies_file', app_config.get('cookies_file'))
    gallerydl.setdefault('cookies_browser', app_config.get('cookies_browser'))
    gallerydl.setdefault('retries', app_config.get('retries'))
    gallerydl.setdefault('sleep', app_config.get('min_sleep'))
    gallerydl.setdefault('timeout_seconds', app_config.get('timeout_seconds'))
    gallerydl.setdefault('tracker_db', app_config.get('tracker_db'))
    gallerydl.setdefault('tracker_json_backup', app_config.get('tracker_json_backup'))
    gallerydl.setdefault('user_agent', app_config.get('user_agent'))

    merged_providers['gallerydl'] = gallerydl
    return merged_providers


def load_config(base_dir: Optional[Path] = None) -> AppConfig:
    """Load configuration from .env plus providers.yaml."""
    base = base_dir or Path(__file__).resolve().parent.parent
    _load_env(base)

    providers_path = base / 'configs' / 'providers.yaml'
    raw_providers = load_yaml(providers_path) if providers_path.exists() else {}

    app_values = {
        'output_root': os.environ.get('TIKTOK_OUTPUT_ROOT', os.environ.get('OUTPUT_ROOT', 'downloads')),
        'log_level': os.environ.get('TIKTOK_LOG_LEVEL', os.environ.get('LOG_LEVEL', 'INFO')),
        'cookies_file': os.environ.get('TIKTOK_COOKIES_FILE', 'configs/tiktok_cookies.txt'),
        'cookies_browser': os.environ.get('TIKTOK_COOKIES_BROWSER') or None,
        'tracker_db': os.environ.get('TIKTOK_DB_PATH', 'data/tiktok_toolkit.db'),
        'tracker_json_backup': os.environ.get('TIKTOK_TRACKER_JSON_BACKUP', 'configs/download_tracker.json.backup'),
        'min_sleep': float(os.environ.get('TIKTOK_MIN_SLEEP', os.environ.get('MIN_SLEEP', 0.5))),
        'max_sleep': float(os.environ.get('TIKTOK_MAX_SLEEP', os.environ.get('MAX_SLEEP', 2.0))),
        'retries': int(os.environ.get('TIKTOK_RETRIES', os.environ.get('RETRIES', 2))),
        'timeout_seconds': int(os.environ.get('TIKTOK_TIMEOUT_SECONDS', os.environ.get('TIMEOUT', 300))),
        'user_agent': os.environ.get(
            'TIKTOK_USER_AGENT',
            os.environ.get('USER_AGENT', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'),
        ),
    }
    app_values['providers'] = _merge_gallerydl_config(app_values, raw_providers)

    raw_spider = raw_providers.get('spider', {})
    app_values['spider_max_following'] = int(
        raw_spider.get('max_following', os.environ.get('TIKTOK_SPIDER_MAX_FOLLOWING', 500))
    )
    app_values['spider_max_followers'] = int(
        raw_spider.get('max_followers', os.environ.get('TIKTOK_SPIDER_MAX_FOLLOWERS', 500))
    )

    gallerydl = app_values['providers'].setdefault('gallerydl', {})
    gallerydl['tracker_required'] = _env_bool('TIKTOK_TRACKER_REQUIRED', bool(gallerydl.get('tracker_required', True)))
    gallerydl['skip_existing'] = _env_bool('TIKTOK_SKIP_EXISTING', bool(gallerydl.get('skip_existing', True)))
    gallerydl['browser_fallback_enabled'] = _env_bool(
        'TIKTOK_BROWSER_FALLBACK_ENABLED', bool(gallerydl.get('browser_fallback_enabled', True))
    )
    gallerydl['ytdlp_fallback_enabled'] = _env_bool(
        'TIKTOK_YTDLP_FALLBACK_ENABLED', bool(gallerydl.get('ytdlp_fallback_enabled', True))
    )

    return AppConfig(**app_values)
