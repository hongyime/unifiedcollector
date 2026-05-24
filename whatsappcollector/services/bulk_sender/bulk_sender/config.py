from __future__ import annotations

from shared.config import BaseConfig


class Settings(BaseConfig):
    SERVICE_NAME: str = "bulk_sender"
    MEDIA_BRIDGE_URL: str = ""
    MEDIA_BRIDGE_SECRET: str = ""
    MEDIA_BRIDGE_PORT: int = 3001
    SESSION_BRIDGES_JSON: str = ""
    BULK_SENDER_INTERNAL_TARGET_JID: str = ""
    BULK_SENDER_INTERNAL_MIN_DELAY: float = 2.0
    BULK_SENDER_EXTERNAL_MIN_DELAY: float = 8.0
    BULK_SENDER_EXTERNAL_MAX_PER_HOUR: int = 30
    BULK_SENDER_MAX_EXTERNAL_TARGETS: int = 20
    BULK_SENDER_MEMBERSHIP_MIN_AGE_HOURS: int = 48
    BULK_SENDER_PROMETHEUS_PORT: int = 9095
    BULK_SENDER_POLL_INTERVAL_SEC: int = 5


settings = Settings()
