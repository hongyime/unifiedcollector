from pathlib import Path
import re
import shlex

import yaml


def _resolve_compose_default(value: str, environment: dict[str, str]) -> str:
    """Resolve a `${VAR:-default}` reference against the service environment."""
    match = re.fullmatch(r"\$\{([A-Z_][A-Z0-9_]*):-([^}]*)\}", value)
    if not match:
        return value
    name, default = match.groups()
    return environment.get(name, default)


def test_postgres_uses_init_to_isolate_orphan_exec_processes() -> None:
    path = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"
    postgres = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["postgres"]

    assert postgres.get("init") is True, (
        "Postgres must not adopt orphan exec clients as PID 1: their exit 2 or "
        "SIGPIPE incorrectly triggers crash recovery for every database"
    )


def test_postgres_healthcheck_selects_the_configured_database() -> None:
    path = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"
    postgres = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["postgres"]
    command = shlex.split(postgres["healthcheck"]["test"][1])

    assert "-d" in command, "pg_isready must not probe the nonexistent collector database"
    database = _resolve_compose_default(command[command.index("-d") + 1], postgres["environment"])
    assert database == postgres["environment"]["POSTGRES_DB"]
