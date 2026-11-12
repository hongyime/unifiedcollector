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


def test_spiderfoot_healthcheck_probes_worker_progress_and_database() -> None:
    block = _service_block("collector_spiderfoot")
    assert 'src.recon_spiderfoot_service", "--healthcheck' in block
    assert "sf.py -V" not in block


def test_spiderfoot_keeps_private_evidence_and_isolated_guard_without_admission_gates() -> None:
    block = _service_block("collector_spiderfoot")
    assert 'user: "${RECON_UID:-1000}:${RECON_GID:-1000}"' in block
    assert 'RECON_EVIDENCE_ROOT: "/var/lib/unifiedcollector/recon-evidence"' in block
    assert "/var/lib/unifiedcollector/recon-evidence:/var/lib/unifiedcollector/recon-evidence" in block
    assert 'MAIGRET_REPORT_KEEPDIR: "${MAIGRET_REPORT_KEEPDIR:-/var/lib/unifiedcollector/recon-evidence/maigret}"' in block
    assert 'network_mode: "service:recon_egress_guard"' in block
    assert "recon_egress_guard:" in block
    assert 'condition: service_healthy' in block
    assert "RECON_ADMISSION_" not in block
    assert "/var/lib/unifiedcollector/recon-admission" not in block
    assert "/var/lib/unifiedcollector/recon-guard:/var/lib/unifiedcollector/recon-guard:ro" in block


def test_recon_seed_scheduler_shares_the_admission_gate_without_evidence_access() -> None:
    block = _service_block("scheduler")
    assert 'group_add:' in block
    assert 'RECON_ADMISSION_POLICY_PATH: "/app/config/task-37-admission-policy.json"' in block
    assert 'RECON_ADMISSION_STATE_PATH: "/var/lib/unifiedcollector/recon-admission/task-37-admission-state.json"' in block
    assert "/var/lib/unifiedcollector/recon-admission:/var/lib/unifiedcollector/recon-admission" in block
    assert "/var/lib/unifiedcollector/recon-evidence:/var/lib/unifiedcollector/recon-evidence" not in block
