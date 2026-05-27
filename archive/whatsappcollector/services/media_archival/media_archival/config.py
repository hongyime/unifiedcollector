from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field
from shared.config import BaseConfig


class Settings(BaseConfig):
    SERVICE_NAME: str = "media_archival"

    # Storage / bridge
    MEDIA_STORAGE_PATH: str = "/data/media"
    MEDIA_BRIDGE_URL: str = "http://wa-client-ts-1:3001"
    MEDIA_BRIDGE_SECRET: str = Field(default="", repr=False)
    SESSION_BRIDGES_JSON: str = ""

    # Cleanup / redownload cadence
    MEDIA_CLEANUP_INTERVAL_HOURS: int = 24
    MEDIA_RETENTION_DAYS: int = 90
    MEDIA_REDOWNLOAD_ENABLED: bool = False
    MEDIA_REDOWNLOAD_LOOKAHEAD_HOURS: int = 2
    MEDIA_REDOWNLOAD_INTERVAL_SECONDS: int = 1800

    # Worker loop cadence
    MEDIA_ARCHIVAL_POLL_SECONDS: int = 5
    MEDIA_ARCHIVAL_BATCH_SIZE: int = 50
    MEDIA_ARCHIVAL_MAX_DOWNLOAD_RETRIES: int = 3

    # Database connection pool
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800

    @property
    def storage_path(self) -> Path:
        return Path(self.MEDIA_STORAGE_PATH)

    @property
    def wa_clients(self) -> dict[str, str]:
        """Map session_name -> wa-client base URL, parsed from SESSION_BRIDGES_JSON."""
        mapping: dict[str, str] = {}
        raw = (self.SESSION_BRIDGES_JSON or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    for key, value in parsed.items():
                        if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip():
                            mapping[key.strip()] = value.strip().rstrip("/")
            except Exception:
                mapping = {}
        return mapping


settings = Settings()
