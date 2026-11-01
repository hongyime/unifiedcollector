"""Regression guards for T2 and T3 of the security-findings plan.

T2 — dashboard loopback + auth interpolation: the dashboard publishes its
ports on host loopback only and its auth-disable switch stays an explicit
environment interpolation (default ``true``) so a deployment can fail closed
without editing the compose file.

T3 — bridge/database loopback + required secrets: the WhatsApp bridges, the
browser ingest bridge and Postgres publish only on ``127.0.0.1``, and every
secret Compose needs to start the broker/db/bridges uses the ``:?`` required
form so a missing variable aborts ``docker compose up`` (fail closed) instead
of silently starting with an empty or default password.

Why these invariants matter: this is a single-user rig on a LAN. Publishing a
bare port puts an unauthenticated (or default-password) service on every
interface the host has, and a secret with a fallback quietly runs the stack
with a known credential. Both are LAN-isolation failures, so they get tests,
not comments.
"""

from pathlib import Path

import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"
VAULT_PATH = Path(__file__).resolve().parents[2] / "src" / "tools" / "browser_cookie_vault.py"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _assert_ports(service_name: str, expected: list[str]) -> None:
    ports = _compose()["services"][service_name]["ports"]
    assert sorted(ports) == sorted(expected), (
        f"{service_name} must publish only {expected!r} (host loopback), got {ports!r}"
    )
    assert len(ports) == len(expected)


def test_dashboard_ports_are_loopback_only() -> None:
    _assert_ports("dashboard", ["127.0.0.1:8001:8700", "127.0.0.1:8700:8700"])


def test_browser_ingest_port_is_loopback_only() -> None:
    _assert_ports("browser_ingest", ["127.0.0.1:8765:8765"])


def test_wa_bridge_1_port_is_loopback_only() -> None:
    _assert_ports("wa-bridge-1", ["127.0.0.1:3011:3001"])


def test_wa_bridge_2_port_is_loopback_only() -> None:
    _assert_ports("wa-bridge-2", ["127.0.0.1:3012:3001"])


def test_postgres_port_is_loopback_only_and_configurable() -> None:
    _assert_ports("postgres", ["127.0.0.1:${POSTGRES_HOST_PORT:-5433}:5432"])


def test_dashboard_auth_disable_stays_an_explicit_env_interpolation() -> None:
    environment = _compose()["services"]["dashboard"]["environment"]
    assert environment["DASHBOARD_AUTH_DISABLED"] == "${DASHBOARD_AUTH_DISABLED:-true}", (
        "dashboard auth must remain an operator-controlled env switch, not a "
        "hardcoded bypass or a silent default that cannot be turned on"
    )


def test_required_secret_guards_are_fail_closed() -> None:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    for guard in (
        "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}",
        "${RABBITMQ_PASSWORD:?RABBITMQ_PASSWORD required}",
        "${REDIS_PASSWORD:?REDIS_PASSWORD required}",
        "${WHATSAPP_MEDIA_BRIDGE_SECRET:?WHATSAPP_MEDIA_BRIDGE_SECRET required}",
        "${BRIDGE_SHARED_SECRET:?BRIDGE_SHARED_SECRET required}",
    ):
        assert guard in text, f"compose must abort when {guard} is unset"


def test_cookie_vault_health_endpoint_binds_loopback() -> None:
    text = VAULT_PATH.read_text(encoding="utf-8")
    assert 'web.TCPSite(runner, "127.0.0.1", port)' in text
    assert 'web.TCPSite(runner, "0.0.0.0", port)' not in text, (
        "the cookie vault health endpoint must not listen on every interface"
    )


def test_no_service_publishes_a_bare_port() -> None:
    services = _compose()["services"]
    for name, service in services.items():
        for entry in service.get("ports", []):
            if not isinstance(entry, str):
                raise AssertionError(f"{name}: non-string port entry {entry!r}")
            # A container-only port (no colon) is not published on the host.
            # Anything with a host:container mapping must pin host loopback.
            if ":" in entry:
                assert entry.startswith("127.0.0.1:"), (
                    f"{name}: port {entry!r} must bind 127.0.0.1, not every interface"
                )
