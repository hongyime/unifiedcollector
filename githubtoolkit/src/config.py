"""Configuration management for GitHub Toolkit."""
import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Load .env file
load_dotenv()

class Config:
    """Configuration settings loaded from .env file."""
    
    # Paths
    BASE_DIR = Path(__file__).parent.parent
    DATA_DIR = BASE_DIR / "data"
    DOWNLOADS_DIR = BASE_DIR / "downloads"
    AVATARS_DIR = DOWNLOADS_DIR / "avatars"
    DB_PATH = DATA_DIR / "github_toolkit.db"

    # GitHub API
    GITHUB_API_BASE = "https://api.github.com"
    GITHUB_MAX_USERS: int = int(os.getenv("GITHUB_MAX_USERS", "50000"))
    
    # Avatar downloads
    AVATAR_SIZE: int = int(os.getenv("AVATAR_SIZE", "460"))
    AVATAR_CDN_BASE = "https://avatars.githubusercontent.com/u"
    
    # Photo blob storage
    PROFILE_PHOTO_BLOB_MAX_SIZE_MB: int = int(os.getenv("PROFILE_PHOTO_BLOB_MAX_SIZE_MB", "5000"))
    
    # Rate limiting
    API_RATE_LIMIT_BUFFER: int = int(os.getenv("API_RATE_LIMIT_BUFFER", "10"))
    AVATAR_DOWNLOAD_DELAY: float = float(os.getenv("AVATAR_DOWNLOAD_DELAY", "0.5"))
    API_REQUEST_DELAY: float = float(os.getenv("API_REQUEST_DELAY", "0.1"))
    
    # Concurrency
    MAX_CONCURRENT_DOWNLOADS: int = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "10"))
    MAX_CONCURRENT_API_REQUESTS: int = int(os.getenv("MAX_CONCURRENT_API_REQUESTS", "5"))
    
    # Spider settings
    DEFAULT_SPIDER_DEPTH: int = int(os.getenv("DEFAULT_SPIDER_DEPTH", "3"))
    
    # Flask settings
    FLASK_HOST: str = os.getenv("FLASK_HOST", "127.0.0.1")
    FLASK_PORT: int = int(os.getenv("FLASK_PORT", "5000"))
    FLASK_DEBUG: bool = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    
    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: Path = BASE_DIR / "logs" / "github_toolkit.log"
    
    @classmethod
    def ensure_directories(cls):
        """Create necessary directories if they don't exist."""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        cls.AVATARS_DIR.mkdir(parents=True, exist_ok=True)
        cls.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    @classmethod
    def validate(cls) -> list[str]:
        """Validate configuration and return list of warnings."""
        warnings = []
        
        if cls.GITHUB_MAX_USERS > 100000:
            warnings.append(f"GITHUB_MAX_USERS is very high ({cls.GITHUB_MAX_USERS}). This may take a long time.")
        
        if cls.MAX_CONCURRENT_DOWNLOADS > 50:
            warnings.append(f"MAX_CONCURRENT_DOWNLOADS is very high ({cls.MAX_CONCURRENT_DOWNLOADS}). May hit rate limits.")
        
        return warnings

# Initialize directories on import
Config.ensure_directories()
