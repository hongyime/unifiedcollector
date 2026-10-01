"""The production stack must not keep an idle all-sources worker container.

The ``worker --all`` CLI stays. Dedicated per-source services already build
``unifiedcollector-collector:latest``. The bare ``collector`` service only
disabled every source and held 512m.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker" / "docker-compose.yml"
CLI = REPO_ROOT / "src" / "cli" / "commands" / "core_ops.py"
STARTUP = REPO_ROOT / "scripts" / "register-collector-startup.ps1"

_BARE_CONTAINER = re.compile(r"unifiedcollector_collector(?!_)")
_BARE_SERVICE = re.compile(r"^  collector:\s*$", re.MULTILINE)


def test_production_compose_has_no_idle_all_worker() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert _BARE_SERVICE.search(text) is None
    assert "command: python -m src.main worker --all" not in text
    assert "container_name: unifiedcollector_collector\n" not in text


def test_worker_all_cli_still_exists() -> None:
    text = CLI.read_text(encoding="utf-8")
    assert 'add_parser("worker"' in text
    assert 'add_argument("--all"' in text


def test_startup_script_does_not_target_removed_idle_collector() -> None:
    text = STARTUP.read_text(encoding="utf-8")
    assert _BARE_CONTAINER.search(text) is None
    assert "unifiedcollector_collector_telegram" in text
    assert "unifiedcollector_scheduler" in text
