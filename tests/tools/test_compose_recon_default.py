"""Spiderfoot starts with the production stack. Instagram DM stays opt-in."""
from __future__ import annotations

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"


def _service_block(name: str) -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert match is not None, name
    return match.group(1)


def test_spiderfoot_starts_without_a_compose_profile() -> None:
    block = _service_block("collector_spiderfoot")
    assert "profiles:" not in block
    assert 'profiles: ["recon"]' not in COMPOSE.read_text(encoding="utf-8")


def test_instagram_dm_stays_opt_in() -> None:
    block = _service_block("collector_instagram_dm")
    assert 'profiles: ["instagram-dm"]' in block
