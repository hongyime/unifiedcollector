from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "config" / "zero-spend-capability-matrix.json"
COMPOSE = ROOT / "docker" / "docker-compose.yml"


def test_zero_spend_matrix_covers_every_compose_collector_and_active_engine():
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = {
        name for name in compose["services"]
        if name.startswith("collector_") or name == "browser_ingest"
    }
    covered = {row["service"] for row in matrix["providers"]}
    assert services <= covered
    assert matrix["data_use_owner"] == {
        "status": "user-designated",
        "owner": "the user (repository operator)",
    }
    assert len(matrix["prohibited_conduct"]) >= 5
    assert all(
        {"provider", "charge_status", "credentials_required", "effective_enablement", "zero_spend_reason"}
        <= row.keys()
        for row in matrix["providers"]
    )
    assert any(
        row["provider"] == "Serper Google SERP API"
        and row["effective_enablement"] == "disabled and explicit engine requests rejected"
        for row in matrix["providers"]
    )
    assert any(
        row["provider"].startswith("Paid/uncertain SpiderFoot modules")
        and row["effective_enablement"] == "disabled and rejected by PAID_OR_UNCERTAIN_MODULES"
        for row in matrix["providers"]
    )
