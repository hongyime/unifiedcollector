"""
Enhanced Configuration Management System
Supports both .env files (preferred) and config.json (legacy)
"""
import json
import os
from pathlib import Path
from typing import Any, Dict

try:
    from dotenv import load_dotenv
    # Load .env from toolkit root
    load_dotenv(Path(__file__).parent.parent / '.env')
    _DOTENV_AVAILABLE = True
except ImportError:
    _DOTENV_AVAILABLE = False

from app_paths import CONFIG_FILE, DEFAULT_DOWNLOADS_DIR


class ConfigManager:
    def __init__(self, config_file: str = str(CONFIG_FILE)):
        self.config_file = config_file
        self.default_config = {
            'processing': {
                'batch_size': 10,
                'max_retries': 3,
                'timeout_seconds': 300
            },
            'output': {
                'base_folder': str(DEFAULT_DOWNLOADS_DIR),
                'organize_by_date': True
            },
            'download': {
                'max_resolution': '1080',
                'audio_only': False,
                'max_video_duration_minutes': 0,  # 0 = no limit
                'delay_seconds': 5.0
            },
            'cookies': {
                'use_cookies': True,
                'browser': 'auto'  # auto, chrome, edge, firefox
            },
            'ui': {
                'show_progress': True,
                'colorful_output': True,
                'auto_cleanup': False
            },
            'api': {
                'youtube_api_key': '',
                'rate_limit_delay': 1.0,
            }
        }
        self.config = self.load_config()
    
    def _load_from_env(self) -> Dict[str, Any]:
        """Load configuration from .env file (preferred method)."""
        if not _DOTENV_AVAILABLE:
            return {}
        
        return {
            'log_level': os.environ.get('YOUTUBE_LOG_LEVEL', 'INFO'),
            'api': {
                'youtube_api_key': os.environ.get('YOUTUBE_API_KEY', ''),
                'daily_quota_limit': int(os.environ.get('YOUTUBE_DAILY_QUOTA_LIMIT', 10000)),
                'rate_limit_delay': float(os.environ.get('YOUTUBE_API_DELAY_SECONDS', 1.0)),
            },
            'oauth': {
                'client_id': os.environ.get('YOUTUBE_CLIENT_ID', ''),
                'client_secret': os.environ.get('YOUTUBE_CLIENT_SECRET', ''),
            },
            'output': {
                'base_folder': os.environ.get('YOUTUBE_DOWNLOAD_PATH', 'downloads'),
            },
            'download': {
                'max_concurrent': int(os.environ.get('YOUTUBE_MAX_CONCURRENT_DOWNLOADS', 3)),
                'delay_seconds': float(os.environ.get('YOUTUBE_DOWNLOAD_DELAY_SECONDS', 5.0)),
                'ytdlp_format': os.environ.get('YOUTUBE_YTDLP_FORMAT', 'best'),
                'ytdlp_retries': int(os.environ.get('YOUTUBE_YTDLP_RETRIES', 3)),
                'max_video_duration_minutes': int(os.environ.get('YOUTUBE_MAX_VIDEO_DURATION_MINUTES', 0)),
            },
            'cookies': {
                'use_cookies': os.environ.get('YOUTUBE_USE_COOKIES', 'true').lower() == 'true',
                'browser': os.environ.get('YOUTUBE_COOKIE_BROWSER', 'auto'),
            },
            'profile_photo': {
                'blob_max_size_mb': int(os.environ.get('YOUTUBE_PROFILE_PHOTO_BLOB_MAX_SIZE_MB', 5000)),
            },
            'database': {
                'path': os.environ.get('YOUTUBE_DB_PATH', 'data/youtube_data.db'),
            },
            'web': {
                'host': os.environ.get('YOUTUBE_WEB_HOST', '127.0.0.1'),
                'port': int(os.environ.get('YOUTUBE_WEB_PORT', 5000)),
            }
        }
    
    def load_config(self) -> Dict[str, Any]:
        """Load configuration from .env (preferred) and config.json (legacy)"""
        # Start with defaults
        config = self.default_config.copy()
        
        # Load from .env if available (preferred)
        if _DOTENV_AVAILABLE:
            env_config = self._load_from_env()
            # Deep merge env_config into config
            for key, value in env_config.items():
                if isinstance(value, dict) and key in config:
                    config[key].update(value)
                else:
                    config[key] = value
        
        # Load from config.json if it exists (legacy, for backward compatibility)
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        loaded_config = json.loads(content)
                        # Merge JSON config (takes precedence over .env for backward compat)
                        for key, value in loaded_config.items():
                            if isinstance(value, dict) and key in config:
                                config[key].update(value)
                            else:
                                config[key] = value
        except Exception as e:
            print(f"⚠️  Error loading config.json: {e}")
        
        return config
    
    def _save_config_to_file(self, config_data: Dict[str, Any]) -> bool:
        """Helper method to save configuration data to file"""
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"❌ Error saving config to file: {e}")
            return False
    
    def save_config(self) -> bool:
        """Save current configuration to file"""
        return self._save_config_to_file(self.config)
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value using dot notation (e.g., 'face_detection.scale_factor')"""
        try:
            keys = key.split('.')
            value = self.config
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default
    
    def set(self, key: str, value: Any) -> bool:
        """Set configuration value using dot notation"""
        try:
            keys = key.split('.')
            config_section = self.config
            
            # Navigate to the parent of the target key
            for k in keys[:-1]:
                if k not in config_section:
                    config_section[k] = {}
                config_section = config_section[k]
            
            # Set the final value
            config_section[keys[-1]] = value
            return True
        except Exception as e:
            print(f"❌ Error setting config {key}={value}: {e}")
            return False
    
    def reset_to_defaults(self):
        """Reset configuration to default values"""
        self.config = self.default_config.copy()
        self.save_config()

# Global config instance
config = ConfigManager()
