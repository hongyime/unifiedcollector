import json

from pydantic import Field
from shared.config import BaseConfig


class Settings(BaseConfig):
    """Collector service configuration."""

    SERVICE_NAME: str = "collector"

    # Runtime
    METRICS_PORT: int = 9090
    DASHBOARD_PORT: int = 8501
    LOG_LEVEL: str = "INFO"

    # Backfill / bridge integration
    WA_CLIENT_BASE_URL: str = Field(default="", alias="COLLECTOR_WA_CLIENT_BASE_URL")
    SESSION_BRIDGES_JSON: str = ""
    SESSION_NAME: str = "session_1"
    SESSION_NAMES: str = "session_1"
    MEDIA_BRIDGE_URL: str = "http://wa-client-ts-1:3001"
    COLLECTOR_BACKFILL_REQ_PER_MIN: int = 5
    COLLECTOR_BACKFILL_POLL_SECONDS: int = 30
    COLLECTOR_MAX_BACKFILL_AGE_DAYS: int = 90

    # Dedup / payload protection
    COLLECTOR_DEDUP_TTL_SECONDS: int = 86400

    # Session health / risk
    SESSION_RISK_THRESHOLD: float = 0.8
    SESSION_COOLDOWN_SECONDS: int = 300
    SESSION_RISK_WINDOW_SECONDS: int = 3600
    SESSION_RISK_MINUTES_HIGH: int = 5
    LANGUAGE_WHITELIST: str = ""

    # Dashboard
    COLLECTOR_QR_POLL_INTERVAL_SEC: int = 5
    DASHBOARD_AUTH_REQUIRED: bool = True
    DASHBOARD_VIEWER_USERNAME: str = "viewer"
    DASHBOARD_VIEWER_PASSWORD: str = ""
    DASHBOARD_OPERATOR_USERNAME: str = "operator"
    DASHBOARD_OPERATOR_PASSWORD: str = ""
    DASHBOARD_ADMIN_USERNAME: str = "admin"
    DASHBOARD_ADMIN_PASSWORD: str = ""

    # Control-plane secret storage
    CONTROL_PLANE_SECRET_KEY: str = ""
    CONTROL_PLANE_SECRET_KEY_ID: str = "local-kek-v1"

    # Service registry seeding (CSV)
    KNOWN_SERVICES: str = "media_archival,face_recognition,user_intelligence,link_discovery,bulk_sender"

    # Database connection pool
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800

    @property
    def wa_client_url(self) -> str:
        return (self.WA_CLIENT_BASE_URL or self.MEDIA_BRIDGE_URL).rstrip("/")

    @property
    def wa_clients(self) -> dict[str, str]:
        """Map session_name -> wa-client base URL.

        Preferred source is SESSION_BRIDGES_JSON, for example:
        {"session_1":"http://wa-client-ts-1:3001","session_2":"http://wa-client-ts-2:3001"}
        """
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

        if mapping:
            return mapping

        session_names = [
            name.strip()
            for name in (self.SESSION_NAMES or self.SESSION_NAME or "session_1").split(",")
            if name.strip()
        ]
        if not session_names:
            session_names = [self.SESSION_NAME or "session_1"]

        # Fallback: map only the primary configured wa-client URL to the first session.
        return {session_names[0]: self.wa_client_url}

    @property
    def wipeable_schemas(self) -> list[str]:
        return [
            "collector",
            "media_archival",
            "face_recognition",
            "user_intelligence",
            "link_discovery",
            "bulk_sender",
        ]

    @property
    def known_services(self) -> list[str]:
        return [s.strip() for s in self.KNOWN_SERVICES.split(",") if s.strip()]


settings = Settings()
