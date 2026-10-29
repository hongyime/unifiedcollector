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


def test_spiderfoot_evidence_is_ext4_root_bind_with_bounded_run_defaults() -> None:
    block = _service_block("collector_spiderfoot")
    assert 'user: "${RECON_UID:-1000}:${RECON_GID:-1000}"' in block
    assert 'RECON_EVIDENCE_ROOT: "/var/lib/unifiedcollector/recon-evidence"' in block
    assert "/var/lib/unifiedcollector/recon-evidence:/var/lib/unifiedcollector/recon-evidence" in block
    assert 'MAIGRET_REPORT_KEEPDIR: "${MAIGRET_REPORT_KEEPDIR:-/var/lib/unifiedcollector/recon-evidence/maigret}"' in block
    assert 'RECON_MAX_TARGETS_PER_RUN: "${RECON_MAX_TARGETS_PER_RUN:-10}"' in block
    assert 'RECON_ADMISSION_POLICY_PATH: "/app/config/task-37-admission-policy.json"' in block
    assert 'RECON_ADMISSION_STATE_PATH: "/var/lib/unifiedcollector/recon-admission/task-37-admission-state.json"' in block
    assert 'RECON_ADMISSION_STATE_MODE: "0660"' in block
    assert 'RECON_EGRESS_GUARD_SOCKET: "/var/lib/unifiedcollector/recon-guard/status.sock"' in block
    assert "/var/lib/unifiedcollector/recon-admission:/var/lib/unifiedcollector/recon-admission" in block
    assert "/var/lib/unifiedcollector/recon-guard:/var/lib/unifiedcollector/recon-guard:ro" in block
    assert "task-37-admission-policy.json:/app/config/task-37-admission-policy.json:ro" in block
    guard = _service_block("recon_egress_guard")
    assert "RECON_MAX_EGRESS_BYTES" in guard
    assert "/var/lib/unifiedcollector/recon-guard:/var/lib/unifiedcollector/recon-guard" in guard


def test_recon_seed_scheduler_shares_the_admission_gate_without_evidence_access() -> None:
    block = _service_block("scheduler")
    assert 'group_add:' in block
    assert 'RECON_ADMISSION_POLICY_PATH: "/app/config/task-37-admission-policy.json"' in block
    assert 'RECON_ADMISSION_STATE_PATH: "/var/lib/unifiedcollector/recon-admission/task-37-admission-state.json"' in block
    assert "/var/lib/unifiedcollector/recon-admission:/var/lib/unifiedcollector/recon-admission" in block
    assert "/var/lib/unifiedcollector/recon-evidence:/var/lib/unifiedcollector/recon-evidence" not in block
