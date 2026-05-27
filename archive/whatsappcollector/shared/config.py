"""
shared/config.py — Base settings class for all whatsappcollector Python services.

Fixes BUG-07: env_file is resolved relative to this file, not relative to CWD at
import time. This makes .env loading work correctly in Docker containers, when running
pytest from any directory, and when scripts are invoked from repo root.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Three levels up from shared/config.py → repo root → .env
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class BaseConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # Shared infrastructure — declared here so every service can read them
    # without re-declaring.  Services subclass and add their own fields.
    # -------------------------------------------------------------------------

    # Database
    DATABASE_URL: str = ""

    # Broker — defaults to rabbitmq (fixes BUG-02: old default was "redis")
    BROKER_TYPE: str = "rabbitmq"
    RABBITMQ_URL: str = ""

    # Redis
    REDIS_URL: str = ""

    # Payload guard (fixes BUG-06)
    MAX_PAYLOAD_BYTES: int = 10 * 1024 * 1024  # 10 MB

    # Observability
    LOG_LEVEL: str = "INFO"
