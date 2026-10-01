"""GitHub runs in its own container. Strava and search stay together."""
from __future__ import annotations

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"


def _service_block(name: str) -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert match is not None, name
    return match.group(1)


def test_github_has_its_own_worker() -> None:
    block = _service_block("collector_github")
    assert "python -m src.main worker --source github\n" in block
    assert "github.env" in block
    assert "strava.env" not in block
    assert "search.env" not in block
    assert "GITHUB_API_DELAY:" in block


def test_lowrisk_no_longer_runs_github() -> None:
    block = _service_block("collector_lowrisk")
    assert "python -m src.main worker --source strava,search\n" in block
    assert "--source github" not in block
    assert "github.env" not in block
    assert "GITHUB_API_DELAY:" not in block
    assert "strava.env" in block
    assert "search.env" in block
