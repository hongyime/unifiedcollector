"""Pytest-wide guard against lifecycle actions on the live UC Docker stack."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


_PRODUCTION_PREFIX = "unifiedcollector_"
_LIFECYCLE_ACTIONS = frozenset({"kill", "restart", "rm", "stop"})


def is_unsafe_docker_command(args: list[str]) -> bool:
    """True only for Docker commands that can disrupt the live UC stack."""
    tokens = [str(arg) for arg in args]
    if not tokens:
        return False
    if tokens[0] == "system" and "prune" in tokens[1:]:
        return True
    if tokens[0] == "compose" and "down" in tokens[1:]:
        return True
    action = tokens[1] if tokens[0] in {"container", "volume", "network"} and len(tokens) > 1 else tokens[0]
    return action in _LIFECYCLE_ACTIONS and any(token.startswith(_PRODUCTION_PREFIX) for token in tokens)


def pytest_sessionstart(session) -> None:
    """Put a narrow Docker safety shim ahead of the real client for test children."""
    if os.environ.get("UC_PYTEST_ALLOW_LIVE_DOCKER") == "1":
        return
    docker = shutil.which("docker")
    if not docker:
        return
    shim_dir = Path(tempfile.mkdtemp(prefix="uc-pytest-docker-"))
    shim = shim_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "sys.path.insert(0, os.environ['UC_PYTEST_REPO_ROOT'])\n"
        "from tests.conftest import is_unsafe_docker_command\n"
        "if is_unsafe_docker_command(sys.argv[1:]):\n"
        "    raise SystemExit('refusing live UnifiedCollector Docker lifecycle command from pytest')\n"
        "os.execv(os.environ['UC_PYTEST_REAL_DOCKER'], [os.environ['UC_PYTEST_REAL_DOCKER'], *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    shim.chmod(0o700)
    session.config._uc_pytest_docker_shim = shim_dir
    os.environ["UC_PYTEST_REAL_DOCKER"] = docker
    os.environ["UC_PYTEST_REPO_ROOT"] = str(Path(__file__).resolve().parents[1])
    os.environ["PATH"] = str(shim_dir) + os.pathsep + os.environ.get("PATH", "")


def pytest_sessionfinish(session, exitstatus) -> None:
    shim_dir = getattr(session.config, "_uc_pytest_docker_shim", None)
    if shim_dir:
        shutil.rmtree(shim_dir, ignore_errors=True)
