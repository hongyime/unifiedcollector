from __future__ import annotations

from shared.config import BaseConfig


class Settings(BaseConfig):
    SERVICE_NAME: str = "link_discovery"
    LINK_DISCOVERY_BATCH_SIZE: int = 200
    LINK_DISCOVERY_POLL_INTERVAL_SEC: int = 10
    LINK_DISCOVERY_MAX_JOINS_PER_HOUR: int = 3
    LINK_DISCOVERY_JOIN_DELAY_SECONDS: int = 120
    LINK_DISCOVERY_PROMETHEUS_PORT: int = 9094


settings = Settings()
