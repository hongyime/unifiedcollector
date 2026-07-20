from __future__ import annotations

from pathlib import Path

from src.backup.db_backup import (
    BackupFile,
    RetentionPolicy,
    apply_retention_plan,
    build_retention_plan,
    list_backup_files,
    parse_backup_file,
)


def _dump(tmp_path: Path, stamp: str, *, size: int = 1) -> Path:
    path = tmp_path / f"unifiedcollector_{stamp}.dump"
    path.write_bytes(b"x" * size)
    return path


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
    assert commands[0][-1] == "unifiedcollector"
