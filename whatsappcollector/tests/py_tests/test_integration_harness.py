import os
import subprocess
import time
import urllib.error
import urllib.request

import pytest


RUN_INTEGRATION = os.getenv("RUN_INTEGRATION_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="Set RUN_INTEGRATION_TESTS=1 to execute integration harness tests.",
)


def _env_file_value(key: str, default: str) -> str:
    env_path = ".env"
    if not os.path.exists(env_path):
        return default

    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return default


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _container_name(service: str) -> str:
    project = os.getenv("COMPOSE_PROJECT_NAME") or _env_file_value("COMPOSE_PROJECT_NAME", "whatsappcollector")
    mapping = {
        "postgres": "db",
        "redis": "redis",
        "rabbitmq": "rabbitmq",
        "wa-client-ts-1": "client-ts-1",
        "collector": "collector",
    }
    suffix = mapping.get(service, service)
    return f"{project}_{suffix}"


def _wait_for_container_health(service: str, timeout_seconds: int = 120) -> None:
    container = _container_name(service)
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        inspect = _run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            check=False,
        )
        status = (inspect.stdout or "").strip().lower()
        if status == "healthy":
            return
        time.sleep(2)

    raise AssertionError(f"Container {container} did not reach healthy state in time.")


def _wait_for_container_running(service: str, timeout_seconds: int = 120) -> None:
    container = _container_name(service)
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        inspect = _run(
            ["docker", "inspect", "--format", "{{.State.Status}}", container],
            check=False,
        )
        status = (inspect.stdout or "").strip().lower()
        if status == "running":
            return
        time.sleep(2)

    raise AssertionError(f"Container {container} did not reach running state in time.")


def _wait_for_media_bridge(timeout_seconds: int = 120) -> None:
    deadline = time.time() + timeout_seconds
    url = "http://localhost:3011/health"

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status in (200, 503):
                    return
        except urllib.error.HTTPError as exc:
            if exc.code in (200, 503):
                return
        except Exception:
            pass
        time.sleep(2)

    raise AssertionError("Media bridge endpoint was not reachable on http://localhost:3011")


@pytest.fixture(scope="module", autouse=True)
def integration_stack():
    _run(["docker", "compose", "up", "-d", "postgres", "redis", "rabbitmq", "wa-client-ts-1"])

    _wait_for_container_health("postgres")
    _wait_for_container_health("redis")
    _wait_for_container_health("rabbitmq")
    _wait_for_container_running("wa-client-ts-1")
    _wait_for_media_bridge()

    # Ensure schema is present for assertions; init script is idempotent.
    _run(
        [
            "docker", "compose", "exec", "-T", "postgres", "sh", "-lc",
            "psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -f /docker-entrypoint-initdb.d/init-db.sql",
        ]
    )

    yield

    _run(["docker", "compose", "stop", "wa-client-ts-1", "rabbitmq", "redis", "postgres"], check=False)


def test_broker_dependencies_are_ready():
    rabbit_ping = _run(
        ["docker", "compose", "exec", "-T", "rabbitmq", "rabbitmq-diagnostics", "-q", "ping"]
    )
    assert "Ping succeeded" in rabbit_ping.stdout

    redis_password = os.getenv("REDIS_PASSWORD") or _env_file_value("REDIS_PASSWORD", "wac_redis_pass")
    redis_ping = _run(
        ["docker", "compose", "exec", "-T", "redis", "redis-cli", "-a", redis_password, "ping"]
    )
    assert "PONG" in redis_ping.stdout


def test_schema_and_hnsw_index_exist():
    collector_tables_sql = (
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema='collector' "
        "AND table_name IN ('users','chats','raw_messages','jid_lid_map','user_sightings');"
    )

    collector_table_count = _run(
        [
            "docker", "compose", "exec", "-T", "postgres", "sh", "-lc",
            f"psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -tAc \"{collector_tables_sql}\"",
        ]
    )
    assert int(collector_table_count.stdout.strip()) >= 5

    face_tables_sql = (
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema='face_recognition' "
        "AND table_name IN ('face_embeddings','identity_entities');"
    )
    face_table_count = _run(
        [
            "docker", "compose", "exec", "-T", "postgres", "sh", "-lc",
            f"psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -tAc \"{face_tables_sql}\"",
        ]
    )
    assert int(face_table_count.stdout.strip()) == 2

    index_sql = (
        "SELECT COUNT(*) FROM pg_indexes "
        "WHERE schemaname='face_recognition' AND indexname='idx_identity_centroid';"
    )
    index_count = _run(
        [
            "docker", "compose", "exec", "-T", "postgres", "sh", "-lc",
            f"psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -tAc \"{index_sql}\"",
        ]
    )
    assert int(index_count.stdout.strip()) == 1


def test_media_bridge_contract_rejects_unsigned_requests():
    payload = b"{}"
    request = urllib.request.Request(
        "http://localhost:3011/media/decrypt",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=10)

    assert exc_info.value.code == 401
