from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.backup import restore_drill as rd


NOW = datetime(2026, 7, 24, 12, 30, 45, tzinfo=timezone.utc)


def test_default_scratch_database_name_is_guarded():
    name = rd.default_scratch_database_name(NOW)

    assert name == "uc_restore_drill_20260724_123045"
    assert rd.validate_scratch_database_name(name) == name


@pytest.mark.parametrize("name", ["unifiedcollector", "postgres", "bad-name", "uc_restore_drill", "x"])
def test_validate_scratch_database_name_rejects_unsafe_names(name: str):
    with pytest.raises(rd.RestoreDrillError):
        rd.validate_scratch_database_name(name)


def test_select_backup_path_uses_latest_nonempty_dump(tmp_path: Path):
    older = tmp_path / "unifiedcollector_20260723_010000.dump"
    newer = tmp_path / "unifiedcollector_20260724_010000.dump"
    older.write_bytes(b"old")
    newer.write_bytes(b"new")

    config = rd.RestoreDrillConfig(
        database_url="postgresql://collector:secret@localhost:5500/unifiedcollector",
        backup_dir=tmp_path,
    )

    assert rd.select_backup_path(config) == newer


def test_pg_restore_command_does_not_put_password_in_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(rd.shutil, "which", lambda name: name if name == "pg_restore" else None)
    dump = tmp_path / "dump.dump"
    dump.write_bytes(b"dump")
    config = rd.RestoreDrillConfig(
        database_url="postgresql://collector:supersecret@postgres:5432/unifiedcollector?sslmode=disable",
        backup_dir=tmp_path,
    )

    cmd, env = rd.pg_restore_command(config, dump, "uc_restore_drill_20260724_123045")

    assert "supersecret" not in " ".join(cmd)
    assert env["PGPASSWORD"] == "supersecret"
    assert "--host" in cmd
    assert "postgres" in cmd
    assert "--username" in cmd
    assert "collector" in cmd


@pytest.mark.asyncio
async def test_restore_drill_drops_scratch_when_restore_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    calls: list[str] = []
    dump = tmp_path / "unifiedcollector_20260724_010000.dump"
    dump.write_bytes(b"dump")

    async def fake_create(_database_url: str, scratch_database: str):
        calls.append(f"create:{scratch_database}")

    async def fake_drop(_database_url: str, scratch_database: str):
        calls.append(f"drop:{scratch_database}")

    def fake_restore(_config, _backup_path, _scratch_database):
        raise rd.RestoreDrillError("restore blew up")

    monkeypatch.setattr(rd, "_create_scratch_database", fake_create)
    monkeypatch.setattr(rd, "_drop_scratch_database", fake_drop)
    monkeypatch.setattr(rd, "_run_pg_restore", fake_restore)
    config = rd.RestoreDrillConfig(
        database_url="postgresql://collector:secret@localhost:5500/unifiedcollector",
        backup_dir=tmp_path,
        scratch_database="uc_restore_drill_20260724_123045",
    )

    report = await rd.run_restore_drill(config)

    assert report.restored is False
    assert report.dropped_scratch is True
    assert report.error == "restore blew up"
    assert calls == [
        "create:uc_restore_drill_20260724_123045",
        "drop:uc_restore_drill_20260724_123045",
    ]


def test_write_report_creates_parent(tmp_path: Path):
    report = rd.RestoreDrillReport(
        backup_path="/vault/backups/db/unifiedcollector_20260724_010000.dump",
        scratch_database="uc_restore_drill_20260724_123045",
        dry_run=True,
    )

    path = rd.write_report(report, tmp_path / "exports" / "report.json")

    assert path.exists()
    assert "uc_restore_drill_20260724_123045" in path.read_text(encoding="utf-8")
