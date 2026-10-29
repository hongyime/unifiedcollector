"""Static guards preventing automatic resurrection during a cutover."""
from __future__ import annotations

from pathlib import Path
import json


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "config" / "task-43-cutover-controls.json"


def test_compose_watchdog_reads_a_read_only_cutover_marker() -> None:
    text = (ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "CUTOVER_PAUSE_FILE: /var/lib/unifiedcollector/cutover.pause" in text
    assert "/var/lib/unifiedcollector:/var/lib/unifiedcollector:ro" in text


def test_all_restart_paths_honor_an_explicit_pause_marker() -> None:
    freshness = (ROOT / "src" / "watchdog" / "freshness.py").read_text(encoding="utf-8")
    docker = (ROOT / "scripts" / "docker_watchdog.ps1").read_text(encoding="utf-8")
    browser = (ROOT / "scripts" / "browser-autorecover.ps1").read_text(encoding="utf-8")

    assert "def _cutover_pause_active()" in freshness
    assert "if _cutover_pause_active():" in freshness
    assert "cutover pause marker present; skipping all watchdog restart actions" in freshness
    assert 'C:\\unifiedcollector\\cutover.pause' in docker
    assert "Docker Desktop recovery suppressed" in docker
    assert 'Join-Path $repo "cutover.pause"' in browser
    assert 'reason = "cutover_paused"' in browser


def test_cutover_contract_has_scoped_pause_and_restoration_owners() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    assert contract["owner_role"] == "host operator"
    assert set(contract["pause_marker"]) == {"linux", "windows", "semantics"}
    controls = {item["name"]: item for item in contract["pause_controls"]}
    assert set(controls) == {
        "compose_watchdog",
        "docker_desktop_watchdog",
        "browser_autorecover",
        "pg_dump_backup_launcher",
    }
    for item in controls.values():
        assert item["owner_role"]
        assert item["pause_action"]
        assert item["restoration_action"]
        assert "only" in item["scope"].lower() or "no unrelated" in item["scope"].lower()
