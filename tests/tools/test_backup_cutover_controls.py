"""Regression checks for backup behavior during a controlled cutover."""
from __future__ import annotations

from pathlib import Path


LAUNCHER = Path(__file__).resolve().parents[2] / "scripts" / "run_pg_dump_backup.sh"


def test_backup_launcher_never_overrides_an_explicit_disable() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")

    assert '-e "BACKUP_ENABLED=${BACKUP_ENABLED:-1}"' in text
    assert "-e BACKUP_ENABLED=1" not in text
    assert 'if [[ "${BACKUP_ENABLED:-1}" != "1" ]]' in text
    assert 'pause_file="${CUTOVER_PAUSE_FILE:-/var/lib/unifiedcollector/cutover.pause}"' in text
    assert 'if [[ -f "$pause_file" ]]' in text
