from __future__ import annotations

from pathlib import Path


COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"


def test_required_production_secrets_have_no_development_fallbacks() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "localdevrabbit" not in text
    assert "localdevredis" not in text
    assert "localdevmediabridge" not in text
    for name in (
        "RABBITMQ_PASSWORD",
        "REDIS_PASSWORD",
        "WHATSAPP_MEDIA_BRIDGE_SECRET",
        "CONTROL_EXECUTOR_SECRET",
    ):
        assert f"${{{name}:?{name} required}}" in text


def test_dashboard_is_local_auth_disabled_and_loopback_only() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert 'DASHBOARD_AUTH_DISABLED: "${DASHBOARD_AUTH_DISABLED:-true}"' in text
    assert '"127.0.0.1:8001:8700"' in text
    assert '"127.0.0.1:8700:8700"' in text
    assert '"8001:8700"' not in text
    assert '"8700:8700"' not in text
