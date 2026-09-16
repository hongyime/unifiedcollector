from pathlib import Path
import shlex

import yaml


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
    assert command[command.index("-d") + 1] == postgres["environment"]["POSTGRES_DB"]
