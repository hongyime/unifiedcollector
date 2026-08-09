from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.backup.db_backup import (
    BackupAlreadyRunning,
    BackupFile,
    BackupError,
    RetentionPolicy,
    apply_retention_plan,
    assert_backup_mount_ready,
    backup_run_lock,
    backup_status,
    build_retention_plan,
    cleanup_stale_temp_dumps,
    default_backup_dir,
    list_backup_files,
    parse_backup_file,
)


def _dump(tmp_path: Path, stamp: str, *, size: int = 1) -> Path:
    path = tmp_path / f"unifiedcollector_{stamp}.dump"
    path.write_bytes(b"x" * size)
    return path


def _active_lock(tmp_path: Path, *, started_at: str | None = None) -> Path:
    lock = tmp_path / ".backup.lock"
    lock.mkdir()
    payload = {}
    if started_at is not None:
        payload["started_at"] = started_at
    (lock / "owner.json").write_text(json.dumps(payload), encoding="utf-8")
    return lock


def test_parse_backup_file_accepts_expected_name(tmp_path):
    path = _dump(tmp_path, "20260720_033012")

    parsed = parse_backup_file(path)

    assert parsed is not None
    assert parsed.path == path
    assert parsed.created_at.year == 2026
    assert parsed.created_at.month == 7
    assert parsed.created_at.day == 20


def test_list_backup_files_ignores_temp_and_unrelated_files(tmp_path):
    expected = _dump(tmp_path, "20260720_033012")
    _dump(tmp_path, "20260719_033012")
    (tmp_path / ".inprogress_20260720_033012.dump").write_bytes(b"x")
    (tmp_path / "other_20260720_033012.dump").write_bytes(b"x")
    (tmp_path / "unifiedcollector_20260720_033012.sql").write_bytes(b"x")

    backups = list_backup_files(tmp_path)

    assert len(backups) == 2
    assert backups[0].path == expected


def test_default_backup_dir_prefers_collector_vault_root(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    monkeypatch.delenv("COLLECTOR_DB_BACKUP_DIR", raising=False)
    monkeypatch.delenv("COLLECTOR_DB_BACKUP_VAULT_ROOT", raising=False)
    monkeypatch.setenv("COLLECTOR_VAULT_ROOT", str(vault))

    assert default_backup_dir() == vault / "backups" / "db"


def test_backup_status_reports_latest_dump_and_in_progress(tmp_path):
    old = _dump(tmp_path, "20260719_033012", size=2)
    latest = _dump(tmp_path, "20260720_033012", size=5)
    (tmp_path / ".inprogress_20260721_033012.dump").write_bytes(b"x")
    _active_lock(tmp_path)

    status = backup_status(tmp_path, max_age_hours=999999)

    assert status["status"] == "ok"
    assert status["latest_path"] == str(latest)
    assert status["latest_size_bytes"] == 5
    assert status["backup_count"] == 2
    assert status["in_progress"] is True
    assert status["in_progress_count"] == 1
    assert status["in_progress_temp_path"] is not None
    assert status["in_progress_temp_size_bytes"] == 1
    assert status["in_progress_temp_updated_age_seconds"] is not None
    assert status["stale_in_progress_count"] == 0
    assert status["in_progress_recent_max_age_seconds"] == 15 * 60
    assert status["lock_active"] is True
    assert str(old) != status["latest_path"]


def test_backup_status_treats_quiet_temp_dump_as_stale_not_running(tmp_path):
    _dump(tmp_path, "20260720_033012", size=5)
    quiet_temp = tmp_path / ".inprogress_20260721_033012.dump"
    quiet_temp.write_bytes(b"x")
    old = time.time() - (30 * 60)
    os.utime(quiet_temp, (old, old))

    status = backup_status(tmp_path, max_age_hours=999999)

    assert status["status"] == "ok"
    assert status["in_progress"] is False
    assert status["in_progress_count"] == 0
    assert status["stale_in_progress_count"] == 1
    assert status["stale_in_progress_oldest_age_seconds"] >= 30 * 60 - 5


def test_backup_status_does_not_treat_old_temp_dump_as_running(tmp_path):
    _dump(tmp_path, "20260720_033012", size=5)
    stale_temp = tmp_path / ".inprogress_20260721_033012.dump"
    stale_temp.write_bytes(b"x")
    old = time.time() - (8 * 3600)
    os.utime(stale_temp, (old, old))

    status = backup_status(tmp_path, max_age_hours=999999)

    assert status["status"] == "ok"
    assert status["in_progress"] is False
    assert status["in_progress_count"] == 0
    assert status["stale_in_progress_count"] == 1
    assert status["stale_in_progress_oldest_age_seconds"] >= 8 * 3600 - 5


def test_backup_status_marks_old_dump_stale(tmp_path):
    _dump(tmp_path, "20200101_000000")

    status = backup_status(tmp_path, max_age_hours=1)

    assert status["status"] == "stale"
    assert status["max_age_hours"] == 1


def test_backup_status_marks_stale_dump_refreshing_when_new_dump_active(tmp_path):
    _dump(tmp_path, "20200101_000000")
    temp = tmp_path / ".inprogress_20260721_033012.dump"
    temp.write_bytes(b"x" * 7)
    _active_lock(tmp_path)

    status = backup_status(tmp_path, max_age_hours=1)

    assert status["status"] == "refreshing"
    assert status["in_progress"] is True
    assert status["in_progress_count"] == 1
    assert status["in_progress_temp_path"] == str(temp)
    assert status["in_progress_temp_size_bytes"] == 7
    assert status["latest_path"] is not None


def test_backup_status_marks_empty_dir_missing(tmp_path):
    status = backup_status(tmp_path, max_age_hours=1)

    assert status["status"] == "missing"
    assert status["latest_path"] is None
    assert status["backup_count"] == 0


def test_backup_status_marks_empty_dir_refreshing_when_new_dump_active(tmp_path):
    (tmp_path / ".inprogress_20260721_033012.dump").write_bytes(b"x")
    _active_lock(tmp_path)

    status = backup_status(tmp_path, max_age_hours=1)

    assert status["status"] == "refreshing"
    assert status["latest_path"] is None
    assert status["backup_count"] == 0
    assert status["in_progress"] is True


def test_backup_status_reports_long_running_active_dump(tmp_path, monkeypatch):
    _dump(tmp_path, "20200101_000000")
    temp = tmp_path / ".inprogress_20260721_033012.dump"
    temp.write_bytes(b"x" * 7)
    started = "2026-07-21T03:30:12"
    _active_lock(tmp_path, started_at=started)
    monkeypatch.setenv("COLLECTOR_DB_BACKUP_LONG_RUNNING_SECONDS", "3600")

    status = backup_status(tmp_path, max_age_hours=1)

    assert status["status"] == "refreshing"
    assert status["in_progress_started_at"] == started
    assert status["in_progress_elapsed_seconds"] is not None
    assert status["in_progress_elapsed_seconds"] > 3600
    assert status["in_progress_long_running"] is True
    assert status["in_progress_long_running_seconds"] == 3600


def test_retention_keeps_newest_daily_weekly_monthly_buckets(tmp_path):
    newest = _dump(tmp_path, "20260720_090000")
    same_day_older = _dump(tmp_path, "20260720_010000")
    previous_day = _dump(tmp_path, "20260719_090000")
    previous_week = _dump(tmp_path, "20260712_090000")
    previous_month = _dump(tmp_path, "20260601_090000")
    older_month = _dump(tmp_path, "20260501_090000")

    backups = list_backup_files(tmp_path)
    plan = build_retention_plan(backups, RetentionPolicy(daily=2, weekly=2, monthly=2))

    keep = {item.path for item in plan.keep}
    prune = {item.path for item in plan.prune}

    assert newest in keep
    assert previous_day in keep
    assert previous_month in keep
    assert same_day_older in prune
    assert previous_week in prune
    assert older_month in prune
    assert "daily:2026-07-20" in plan.reasons[newest]
    assert "monthly:2026-06" in plan.reasons[previous_month]


def test_apply_retention_plan_deletes_only_pruned_dump_files(tmp_path):
    keep_path = _dump(tmp_path, "20260720_090000")
    prune_path = _dump(tmp_path, "20260719_090000")
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("do not touch")
    keep = parse_backup_file(keep_path)
    prune = parse_backup_file(prune_path)
    assert keep is not None
    assert prune is not None

    plan = build_retention_plan(
        [BackupFile(keep_path, keep.created_at), BackupFile(prune_path, prune.created_at)],
        RetentionPolicy(daily=1, weekly=0, monthly=0),
    )

    deleted = apply_retention_plan(plan)

    assert deleted == [prune_path]
    assert keep_path.exists()
    assert not prune_path.exists()
    assert unrelated.exists()


def test_apply_retention_plan_dry_run_does_not_delete(tmp_path):
    keep_path = _dump(tmp_path, "20260720_090000")
    prune_path = _dump(tmp_path, "20260719_090000")
    plan = build_retention_plan(list_backup_files(tmp_path), RetentionPolicy(daily=1, weekly=0, monthly=0))

    deleted = apply_retention_plan(plan, dry_run=True)

    assert deleted == [prune_path]
    assert keep_path.exists()
    assert prune_path.exists()


def test_cleanup_stale_temp_dumps_only_removes_old_inprogress_files(tmp_path):
    stale = tmp_path / ".inprogress_20260720_090000.dump"
    active = tmp_path / ".inprogress_20260720_100000.dump"
    unrelated = tmp_path / "notes.txt"
    stale.write_bytes(b"stale")
    active.write_bytes(b"active")
    unrelated.write_text("do not touch")
    now = time.time()
    os.utime(stale, (now - 7200, now - 7200))
    os.utime(active, (now - 120, now - 120))

    deleted = cleanup_stale_temp_dumps(tmp_path, max_age_minutes=60, now_ts=now)

    assert deleted == [stale]
    assert not stale.exists()
    assert active.exists()
    assert unrelated.exists()


def test_cleanup_stale_temp_dumps_dry_run_does_not_delete(tmp_path):
    stale = tmp_path / ".inprogress_20260720_090000.dump"
    stale.write_bytes(b"stale")
    now = time.time()
    os.utime(stale, (now - 7200, now - 7200))

    deleted = cleanup_stale_temp_dumps(tmp_path, max_age_minutes=60, now_ts=now, dry_run=True)

    assert deleted == [stale]
    assert stale.exists()


def test_backup_run_lock_blocks_parallel_backup(tmp_path):
    with backup_run_lock(tmp_path, stale_seconds=3600):
        with pytest.raises(BackupAlreadyRunning):
            with backup_run_lock(tmp_path, stale_seconds=3600):
                pass


def test_backup_run_lock_recovers_stale_lock(tmp_path):
    lock_dir = tmp_path / ".backup.lock"
    lock_dir.mkdir()
    now = time.time()
    os.utime(lock_dir, (now - 7200, now - 7200))

    with backup_run_lock(tmp_path, stale_seconds=60, now_ts=now) as acquired:
        assert acquired == lock_dir
        assert (lock_dir / "owner.json").exists()

    assert not lock_dir.exists()


def test_backup_run_lock_recovers_abandoned_temp_dump(tmp_path):
    lock_dir = tmp_path / ".backup.lock"
    lock_dir.mkdir()
    temp = tmp_path / ".inprogress_20260810_033021.dump"
    temp.write_bytes(b"partial")
    now = time.time()
    os.utime(lock_dir, (now - 900, now - 900))
    os.utime(temp, (now - 700, now - 700))

    with backup_run_lock(tmp_path, stale_seconds=3600, abandoned_seconds=600, now_ts=now) as acquired:
        assert acquired == lock_dir
        assert (lock_dir / "owner.json").exists()

    assert not lock_dir.exists()


def test_backup_run_lock_does_not_steal_fresh_lock_without_temp(tmp_path):
    lock_dir = tmp_path / ".backup.lock"
    lock_dir.mkdir()
    now = time.time()
    os.utime(lock_dir, (now - 30, now - 30))

    with pytest.raises(BackupAlreadyRunning):
        with backup_run_lock(tmp_path, stale_seconds=3600, abandoned_seconds=600, now_ts=now):
            pass


def test_backup_run_lock_does_not_steal_progressing_temp_dump(tmp_path):
    lock_dir = tmp_path / ".backup.lock"
    lock_dir.mkdir()
    temp = tmp_path / ".inprogress_20260810_033021.dump"
    temp.write_bytes(b"partial")
    now = time.time()
    os.utime(lock_dir, (now - 900, now - 900))
    os.utime(temp, (now - 60, now - 60))

    with pytest.raises(BackupAlreadyRunning):
        with backup_run_lock(tmp_path, stale_seconds=3600, abandoned_seconds=600, now_ts=now):
            pass


def test_run_once_returns_retry_code_when_backup_already_running(tmp_path):
    from src.backup import db_backup

    _active_lock(tmp_path)
    args = SimpleNamespace(
        command="run",
        backup_dir=str(tmp_path),
        daily=7,
        weekly=4,
        monthly=3,
        prefix="unifiedcollector",
        dry_run=False,
        database=None,
        pg_dump="pg_dump",
        pg_restore="pg_restore",
        docker_container=None,
        docker_exe=None,
        json=False,
    )

    assert db_backup.run_once(args) == 2


def test_run_once_skips_dump_when_latest_backup_is_fresh(monkeypatch, tmp_path, capsys):
    from src.backup import db_backup

    stamp = db_backup.datetime.now().strftime(db_backup.TIMESTAMP_FORMAT)
    _dump(tmp_path, stamp)
    monkeypatch.setenv("COLLECTOR_DB_BACKUP_SKIP_IF_FRESH_SECONDS", "86400")
    monkeypatch.setattr(
        db_backup,
        "create_dump",
        lambda *_args, **_kwargs: pytest.fail("create_dump should not run for fresh backups"),
    )
    args = SimpleNamespace(
        command="run",
        backup_dir=str(tmp_path),
        daily=7,
        weekly=4,
        monthly=3,
        prefix="unifiedcollector",
        dry_run=False,
        database=None,
        pg_dump="pg_dump",
        pg_restore="pg_restore",
        docker_container=None,
        docker_exe=None,
        json=False,
    )

    assert db_backup.run_once(args) == 0
    assert "latest verified dump is fresh" in capsys.readouterr().out


def test_pg_dump_prefers_pg_env_over_host_database_url(monkeypatch, tmp_path):
    from src.backup import db_backup

    commands = []
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost:5500/unifiedcollector")
    monkeypatch.setenv("PGHOST", "postgres")
    monkeypatch.setattr(db_backup, "_run", lambda cmd, *_args, **_kwargs: commands.append(cmd))
    monkeypatch.setattr(db_backup, "_ensure_nonempty", lambda _path: None)

    db_backup._run_pg_dump(
        tmp_path / "unifiedcollector_20260720_090000.dump",
        pg_dump_exe="pg_dump",
        database="unifiedcollector",
    )

    assert commands
    assert commands[0][1:4] == ["-Fc", "-Z", "1"]
    assert commands[0][-1] == "unifiedcollector"


def test_pg_dump_honors_configured_compression(monkeypatch, tmp_path):
    from src.backup import db_backup

    commands = []
    monkeypatch.setenv("COLLECTOR_DB_BACKUP_COMPRESSION", "3")
    monkeypatch.setattr(db_backup, "_run", lambda cmd, *_args, **_kwargs: commands.append(cmd))
    monkeypatch.setattr(db_backup, "_ensure_nonempty", lambda _path: None)

    db_backup._run_pg_dump(
        tmp_path / "unifiedcollector_20260720_090000.dump",
        pg_dump_exe="pg_dump",
        database="unifiedcollector",
    )

    assert commands
    assert commands[0][1:4] == ["-Fc", "-Z", "3"]


def test_docker_pg_dump_honors_configured_compression(monkeypatch, tmp_path):
    from src.backup import db_backup

    commands = []
    monkeypatch.setenv("COLLECTOR_DB_BACKUP_COMPRESSION", "0")
    monkeypatch.setattr(db_backup, "_run", lambda cmd, *_args, **_kwargs: commands.append(cmd))
    monkeypatch.setattr(db_backup, "_ensure_nonempty", lambda _path: None)

    db_backup._run_docker_pg_dump(
        tmp_path / ".inprogress_20260720_090000.dump",
        docker_container="postgres",
        docker_exe="docker",
        database="unifiedcollector",
    )

    assert commands
    assert " -Fc -Z 0 " in commands[0][-1]


def test_pg_dump_rejects_invalid_compression(monkeypatch, tmp_path):
    from src.backup import db_backup

    monkeypatch.setenv("COLLECTOR_DB_BACKUP_COMPRESSION", "10")

    with pytest.raises(ValueError, match="must be between 0 and 9"):
        db_backup._run_pg_dump(
            tmp_path / "unifiedcollector_20260720_090000.dump",
            pg_dump_exe="pg_dump",
            database="unifiedcollector",
        )


def test_run_kills_dump_when_progress_file_stalls(tmp_path):
    from src.backup import db_backup

    progress = tmp_path / ".inprogress_20260720_090000.dump"
    progress.write_bytes(b"")
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="no dump progress"):
        db_backup._run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            "pg_dump failed",
            timeout=5,
            progress_path=progress,
            stall_timeout=0.1,
        )

    assert time.monotonic() - started < 3


def test_run_touches_backup_lock_when_dump_progresses(tmp_path):
    from src.backup import db_backup

    progress = tmp_path / ".inprogress_20260720_090000.dump"
    progress.write_bytes(b"")
    lock_dir = tmp_path / ".backup.lock"
    lock_dir.mkdir()
    old = time.time() - 7200
    os.utime(lock_dir, (old, old))

    script = (
        "import pathlib, time; "
        f"p = pathlib.Path({str(progress)!r}); "
        "time.sleep(0.2); "
        "p.write_bytes(b'progress'); "
        "time.sleep(1.2)"
    )
    db_backup._run(
        [sys.executable, "-c", script],
        "pg_dump failed",
        timeout=5,
        progress_path=progress,
        stall_timeout=3,
    )

    assert lock_dir.stat().st_mtime > old


def test_backup_mount_ready_accepts_vault_mirrored_dir(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    backup_dir = vault / "backups" / "db"
    backup_dir.mkdir(parents=True)
    monkeypatch.setenv("COLLECTOR_DB_BACKUP_VAULT_ROOT", str(vault))
    monkeypatch.setenv("COLLECTOR_DB_BACKUP_REQUIRE_VAULT_MIRROR", "1")

    assert_backup_mount_ready(backup_dir)

    assert not list(backup_dir.glob(".backup_mount_check.*"))


def test_backup_mount_ready_rejects_detached_dir(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "backups" / "db").mkdir(parents=True)
    detached = tmp_path / "detached"
    detached.mkdir()
    monkeypatch.setenv("COLLECTOR_DB_BACKUP_VAULT_ROOT", str(vault))
    monkeypatch.setenv("COLLECTOR_DB_BACKUP_REQUIRE_VAULT_MIRROR", "1")

    with pytest.raises(BackupError, match="not linked to vault mirror"):
        assert_backup_mount_ready(detached)
