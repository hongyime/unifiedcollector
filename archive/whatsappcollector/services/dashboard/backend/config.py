"""
backend/config.py — Dashboard service configuration.

Reads from the shared .env file (same one used by all services).
"""
from __future__ import annotations

import json
import os
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Core database / cache
    database_url: str = "postgresql://wac_user:changeme@postgres:5432/whatsappcollector"
    redis_url: str = "redis://:changeme@redis:6379/0"

    # Session bridge configuration (JSON array of {name, url, secret})
    session_bridges_json: str = "[]"

    # Media bridge
    media_bridge_url: str = "http://wa-client-ts-1:3001"
    media_bridge_secret: str = ""

    # RabbitMQ management API
    rabbitmq_management_url: str = "http://rabbitmq:15672"
    rabbitmq_user: str = "wac_user"
    rabbitmq_password: str = "changeme"

    # Per-session WA client base URLs (comma-separated: session_1=url,session_2=url)
    wa_client_urls: str = "session_1=http://wa-client-ts-1:3001,session_2=http://wa-client-ts-2:3001"

    # Service metrics endpoints
    collector_metrics_url: str = "http://collector:9090"
    media_archival_metrics_url: str = "http://media_archival:9091"
    face_recognition_metrics_url: str = "http://face_recognition:9092"
    user_intelligence_metrics_url: str = "http://user_intelligence:9093"
    link_discovery_metrics_url: str = "http://link_discovery:9094"
    bulk_sender_metrics_url: str = "http://bulk_sender:9095"

    # Dashboard auth
    dashboard_auth_required: bool = True
    dashboard_viewer_username: str = "viewer"
    dashboard_viewer_password: str = ""
    dashboard_operator_username: str = "operator"
    dashboard_operator_password: str = ""
    dashboard_admin_username: str = "admin"
    dashboard_admin_password: str = ""

    # Dashboard service itself
    dashboard_port: int = 8700

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def session_bridges(self) -> list[dict]:
        try:
            return json.loads(self.session_bridges_json)
        except (json.JSONDecodeError, TypeError):
            return []

    @property
    def wa_client_url_map(self) -> dict[str, str]:
        """Parse WA_CLIENT_URLS into {session_name: url} dict."""
        result: dict[str, str] = {}
        for part in self.wa_client_urls.split(","):
            part = part.strip()
            if "=" in part:
                name, url = part.split("=", 1)
                result[name.strip()] = url.strip()
        return result

    def get_all_service_targets(self) -> list[dict[str, str]]:
        """Return list of {name, url} for all services to health-check."""
        targets = [
            {"name": "collector", "url": self.collector_metrics_url},
            {"name": "media_archival", "url": self.media_archival_metrics_url},
            {"name": "face_recognition", "url": self.face_recognition_metrics_url},
            {"name": "user_intelligence", "url": self.user_intelligence_metrics_url},
            {"name": "link_discovery", "url": self.link_discovery_metrics_url},
            {"name": "bulk_sender", "url": self.bulk_sender_metrics_url},
        ]
        for session_name, url in self.wa_client_url_map.items():
            targets.append({"name": f"wa-client-{session_name}", "url": url})
        return targets


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
