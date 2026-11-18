import subprocess

from tests.conftest import is_unsafe_docker_command


def test_blocks_only_live_uc_docker_lifecycle_commands():
    assert is_unsafe_docker_command(["stop", "unifiedcollector_postgres"])
    assert is_unsafe_docker_command(["container", "rm", "-f", "unifiedcollector_collector_x"])
    assert is_unsafe_docker_command(["compose", "down"])
    assert is_unsafe_docker_command(["system", "prune", "-f"])
    assert not is_unsafe_docker_command(["ps"])
    assert not is_unsafe_docker_command(["rm", "-f", "uc-recovery-test-fixture"])


def test_pytest_child_cannot_stop_live_uc_postgres():
    result = subprocess.run(
        ["docker", "stop", "unifiedcollector_postgres"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "refusing live UnifiedCollector Docker lifecycle command" in result.stderr
