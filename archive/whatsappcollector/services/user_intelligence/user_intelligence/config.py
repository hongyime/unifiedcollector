from __future__ import annotations

from shared.config import BaseConfig


class Settings(BaseConfig):
    SERVICE_NAME: str = "user_intelligence"
    USER_INTEL_BATCH_SIZE: int = 500
    USER_INTEL_POLL_INTERVAL_SEC: int = 10
    USER_INTEL_PROMETHEUS_PORT: int = 9093

    TRACKED_FIELDS: str = "display_name,push_name,business_name,phone_number,is_business,is_verified,profile_photo"

    @property
    def tracked_fields(self) -> list[str]:
        return [f.strip() for f in self.TRACKED_FIELDS.split(",") if f.strip()]


settings = Settings()
