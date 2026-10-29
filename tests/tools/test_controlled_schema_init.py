"""Static contract for the one-shot production schema initializer."""
from __future__ import annotations

from pathlib import Path

import yaml


COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"


def _services() -> dict[str, dict]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def test_schema_init_is_one_shot_and_waits_for_postgres() -> None:
    init = _services()["schema_init"]

    assert init["command"] == "python -m src.db.dev_init"
    assert init["restart"] == "no"
    assert init["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert "../src:/app/src:ro" in init["volumes"]


def test_every_direct_postgres_consumer_waits_for_schema_init() -> None:
    for name, service in _services().items():
        if name in {"postgres", "schema_init"}:
            continue
        depends_on = service.get("depends_on", {})
        if "postgres" not in depends_on:
            continue
        assert depends_on["schema_init"] == {
            "condition": "service_completed_successfully"
        }, name


def test_spiderfoot_reaches_schema_gate_through_its_egress_guard() -> None:
    services = _services()
    assert services["collector_spiderfoot"]["depends_on"] == {
        "recon_egress_guard": {"condition": "service_healthy"}
    }
    assert services["recon_egress_guard"]["depends_on"]["schema_init"] == {
        "condition": "service_completed_successfully"
    }
