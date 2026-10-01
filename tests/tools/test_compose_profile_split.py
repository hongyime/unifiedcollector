"""The five profile probes no longer share one container."""
from __future__ import annotations

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"
_SOURCES = ("snapchat", "paypal", "airbnb", "bluesky", "pinterest")


def _service_block(name: str) -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert match is not None, name
    return match.group(1)


def test_each_profile_probe_has_its_own_service() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "collector_profile_only:" not in text
    for source in _SOURCES:
        block = _service_block(f"collector_{source}")
        assert f"python -m src.collectors.profile_only_runner --source {source}\n" in block
        assert f"{source.upper()}_PROBE_ENABLED:" in block


def test_profile_services_do_not_enable_the_other_probes() -> None:
    paypal = _service_block("collector_paypal")
    assert "PAYPAL_PROBE_ENABLED:" in paypal
    assert "SNAPCHAT_PROBE_ENABLED:" not in paypal
    assert "AIRBNB_PROBE_ENABLED:" not in paypal

def test_profile_services_share_one_compose_anchor() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(
        r"^x-profile-probe: &profile-probe\n(.*?)(?=^\S|\Z)",
        text,
        re.M | re.S,
    )
    assert match is not None
    anchor = match.group(1)
    assert "image: unifiedcollector-collector:latest" in anchor
    assert "mem_limit: 192m" in anchor
    assert "./env/common.env" in anchor
    assert "PROBE_ENABLED" not in anchor
    for source in _SOURCES:
        block = _service_block(f"collector_{source}")
        assert "<<: *profile-probe" in block
        assert "mem_limit:" not in block
        assert "image:" not in block
