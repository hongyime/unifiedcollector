"""Spiderfoot starts with the production stack; removed collectors stay absent."""
from __future__ import annotations

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"


def _service_block(name: str) -> str | None:
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    return match.group(1) if match is not None else None


def test_spiderfoot_starts_without_a_compose_profile() -> None:
    block = _service_block("collector_spiderfoot")
    assert "profiles:" not in block
    assert 'profiles: ["recon"]' not in COMPOSE.read_text(encoding="utf-8")


def test_instagram_dm_collector_service_is_removed() -> None:
    assert _service_block("collector_instagram_dm") is None
