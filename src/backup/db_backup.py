"""Bounded PostgreSQL dump management for the collector database.

This module is intentionally standalone: it only needs the Python standard
library plus the PostgreSQL client tools already available to backup runners.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence


DEFAULT_BACKUP_DIR = r"Z:\unifiedcollector\backups\db"
DEFAULT_PREFIX = "unifiedcollector"
DEFAULT_DAILY = 7
DEFAULT_WEEKLY = 4
DEFAULT_MONTHLY = 3
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

_BACKUP_RE = re.compile(r"^(?P<prefix>.+)_(?P<stamp>\d{8}_\d{6})\.dump$")


class BackupError(RuntimeError):
    """Raised when a backup cannot be created or validated safely."""


@dataclass(frozen=True)
class BackupFile:
    path: Path
    created_at: datetime


@dataclass(frozen=True)
class RetentionPolicy:
    daily: int = DEFAULT_DAILY
    weekly: int = DEFAULT_WEEKLY
    monthly: int = DEFAULT_MONTHLY

    def __post_init__(self) -> None:
        for field in ("daily", "weekly", "monthly"):
            value = getattr(self, field)
            if value < 0:
                raise ValueError(f"{field} retention cannot be negative")


@dataclass(frozen=True)
class RetentionPlan:
    keep: tuple[BackupFile, ...]
    prune: tuple[BackupFile, ...]
    reasons: dict[Path, tuple[str, ...]]


def default_backup_dir() -> Path:
    configured = os.getenv("COLLECTOR_DB_BACKUP_DIR")
    if configured:
        return Path(configured)
    vault_root = os.getenv("COLLECTOR_DB_BACKUP_VAULT_ROOT") or os.getenv("COLLECTOR_VAULT_ROOT")
    if vault_root:
        return Path(vault_root) / "backups" / "db"
    container_vault = Path("/vault")
    if container_vault.exists() and container_vault.is_dir():
        return container_vault / "backups" / "db"
    return Path(DEFAULT_BACKUP_DIR)


def default_policy() -> RetentionPolicy:
    return RetentionPolicy(
        daily=_env_int("COLLECTOR_DB_BACKUP_DAILY", DEFAULT_DAILY),
        weekly=_env_int("COLLECTOR_DB_BACKUP_WEEKLY", DEFAULT_WEEKLY),
        monthly=_env_int("COLLECTOR_DB_BACKUP_MONTHLY", DEFAULT_MONTHLY),
    )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"env var {name}={raw!r} is not an integer") from exc
    if value < 0:
        raise ValueError(f"env var {name}={value} cannot be negative")
    return value


def parse_backup_file(path: Path, *, prefix: str = DEFAULT_PREFIX) -> BackupFile | None:
    match = _BACKUP_RE.match(path.name)
    if not match or match.group("prefix") != prefix:
        return None
    try:
        created_at = datetime.strptime(match.group("stamp"), TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return BackupFile(path=path, created_at=created_at)


def list_backup_files(backup_dir: Path, *, prefix: str = DEFAULT_PREFIX) -> list[BackupFile]:
    if not backup_dir.exists():
        return []
    files: list[BackupFile] = []
    for path in backup_dir.iterdir():
        if not path.is_file():
            continue
        backup = parse_backup_file(path, prefix=prefix)
        if backup is not None:
            files.append(backup)
    return sorted(files, key=lambda item: item.created_at, reverse=True)


def backup_status(
    backup_dir: Path | None = None,
    *,
    prefix: str = DEFAULT_PREFIX,
    max_age_hours: int | None = None,
) -> dict:
    """Return JSON-safe latest-backup status for dashboards and Telegram."""
    root = backup_dir or default_backup_dir()
    try:
        threshold_hours = max_age_hours if max_age_hours is not None else _env_int(
            "COLLECTOR_DB_BACKUP_MAX_AGE_HOURS",
            30,
        )
        active_temp_minutes = _env_int("COLLECTOR_DB_BACKUP_IN_PROGRESS_ACTIVE_MINUTES", 15)
        backups = list_backup_files(root, prefix=prefix)
        temp_files = sorted(root.glob(".inprogress_*.dump")) if root.exists() else []
    except Exception as exc:
        return {
            "status": "error",
            "root": str(root),
            "latest_path": None,
            "latest_created_at": None,
            "latest_age_seconds": None,
            "latest_size_bytes": None,
            "backup_count": 0,
            "in_progress": False,
            "in_progress_count": 0,
            "stale_in_progress_count": 0,
            "stale_in_progress_oldest_age_seconds": None,
            "in_progress_recent_max_age_seconds": None,
            "max_age_hours": max_age_hours,
            "error": str(exc),
        }

    now = datetime.now()
    active_temp_max_age_seconds = active_temp_minutes * 60
    temp_ages: list[int] = []
    for path in temp_files:
        try:
            temp_ages.append(max(0, int(now.timestamp() - path.stat().st_mtime)))
        except OSError:
            continue
    active_temp_count = sum(
        1
        for age in temp_ages
        if active_temp_max_age_seconds <= 0 or age <= active_temp_max_age_seconds
    )
    stale_temp_ages = [
        age
        for age in temp_ages
        if active_temp_max_age_seconds > 0 and age > active_temp_max_age_seconds
    ]
    temp_payload = {
        "in_progress": active_temp_count > 0,
        "in_progress_count": active_temp_count,
        "stale_in_progress_count": len(stale_temp_ages),
        "stale_in_progress_oldest_age_seconds": max(stale_temp_ages) if stale_temp_ages else None,
        "in_progress_recent_max_age_seconds": active_temp_max_age_seconds,
    }
    latest = backups[0] if backups else None
    if latest is None:
        return {
            "status": "missing",
            "root": str(root),
            "latest_path": None,
            "latest_created_at": None,
            "latest_age_seconds": None,
            "latest_size_bytes": None,
            "backup_count": 0,
            "max_age_hours": threshold_hours,
            "error": None,
            **temp_payload,
        }

    age_seconds = max(0, int((now - latest.created_at).total_seconds()))
    stale = threshold_hours > 0 and age_seconds > threshold_hours * 3600
    try:
        latest_size = latest.path.stat().st_size
    except OSError:
        latest_size = None
    return {
        "status": "stale" if stale else "ok",
        "root": str(root),
        "latest_path": str(latest.path),
        "latest_created_at": latest.created_at.isoformat(),
        "latest_age_seconds": age_seconds,
        "latest_size_bytes": latest_size,
        "backup_count": len(backups),
        "max_age_hours": threshold_hours,
        "error": None,
        **temp_payload,
    }


def build_retention_plan(
    backups: Iterable[BackupFile],
    policy: RetentionPolicy,
) -> RetentionPlan:
    ordered = sorted(backups, key=lambda item: item.created_at, reverse=True)
    reasons: dict[Path, set[str]] = defaultdict(set)

    _mark_newest_per_bucket(
        ordered,
        policy.daily,
        lambda dt: dt.date().isoformat(),
        "daily",
        reasons,
    )
    _mark_newest_per_bucket(
        ordered,
        policy.weekly,
        lambda dt: f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}",
        "weekly",
        reasons,
    )
    _mark_newest_per_bucket(
        ordered,
        policy.monthly,
        lambda dt: f"{dt.year:04d}-{dt.month:02d}",
        "monthly",
        reasons,
    )

    keep_paths = set(reasons)
    keep = tuple(item for item in ordered if item.path in keep_paths)
    prune = tuple(item for item in ordered if item.path not in keep_paths)
    frozen_reasons = {path: tuple(sorted(path_reasons)) for path, path_reasons in reasons.items()}
    return RetentionPlan(keep=keep, prune=prune, reasons=frozen_reasons)


def _mark_newest_per_bucket(
    backups: Sequence[BackupFile],
    limit: int,
    bucket_for: Callable[[datetime], str],
    label: str,
    reasons: dict[Path, set[str]],
) -> None:
    if limit <= 0:
        return
    seen: set[str] = set()
    for backup in backups:
        bucket = bucket_for(backup.created_at)
        if bucket in seen:
            continue
        if len(seen) >= limit:
            break
        seen.add(bucket)
        reasons[backup.path].add(f"{label}:{bucket}")


def apply_retention_plan(plan: RetentionPlan, *, dry_run: bool = False) -> list[Path]:
    deleted: list[Path] = []
    for backup in plan.prune:
        if dry_run:
            deleted.append(backup.path)
            continue
        backup.path.unlink()
        deleted.append(backup.path)
    return deleted


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def assert_backup_mount_ready(backup_dir: Path) -> None:
    """Prepare backup_dir, failing closed when Docker should be on the vault."""
    vault_root_raw = os.getenv("COLLECTOR_DB_BACKUP_VAULT_ROOT")
    require_mirror = _env_bool("COLLECTOR_DB_BACKUP_REQUIRE_VAULT_MIRROR", bool(vault_root_raw))
    if not require_mirror:
        backup_dir.mkdir(parents=True, exist_ok=True)
        return

    if not vault_root_raw:
        raise BackupError("COLLECTOR_DB_BACKUP_VAULT_ROOT is required when vault mirror checks are enabled")

    vault_root = Path(vault_root_raw)
    expected = vault_root / "backups" / "db"
    if not backup_dir.exists() or not backup_dir.is_dir():
        raise BackupError(f"backup dir missing or not a directory: {backup_dir}")
    if not expected.exists() or not expected.is_dir():
        raise BackupError(f"vault backup mirror missing or not a directory: {expected}")

    token = f"{os.getpid()}:{time.time_ns()}"
    name = f".backup_mount_check.{os.getpid()}.{time.time_ns()}"
    probe = backup_dir / name
    expected_probe = expected / name
    try:
        probe.write_text(token, encoding="utf-8")
        if not expected_probe.exists() or expected_probe.read_text(encoding="utf-8") != token:
            raise BackupError(f"backup dir {backup_dir} is not linked to vault mirror {expected}")
    finally:
        for path in {probe, expected_probe}:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def create_dump(
    backup_dir: Path,
    *,
    prefix: str = DEFAULT_PREFIX,
    database: str | None = None,
    pg_dump_exe: str = "pg_dump",
    pg_restore_exe: str = "pg_restore",
    docker_container: str | None = None,
    docker_exe: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> Path:
    now = now or datetime.now()
    ts = now.strftime(TIMESTAMP_FORMAT)
    final = backup_dir / f"{prefix}_{ts}.dump"
    tmp = backup_dir / f".inprogress_{ts}.dump"

    if dry_run:
        return final

    assert_backup_mount_ready(backup_dir)
    if tmp.exists():
        tmp.unlink()

    try:
        if docker_container:
            _run_docker_pg_dump(
                tmp,
                docker_container=docker_container,
                docker_exe=docker_exe or _default_docker_exe(),
                database=database or _default_database(),
            )
            _validate_docker_dump(
                tmp,
                docker_container=docker_container,
                docker_exe=docker_exe or _default_docker_exe(),
            )
        else:
            _run_pg_dump(
                tmp,
                pg_dump_exe=pg_dump_exe,
                database=database or _default_database(),
            )
            _validate_dump(tmp, pg_restore_exe=pg_restore_exe)
        tmp.replace(final)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise

    return final


def _default_database() -> str:
    return os.getenv("COLLECTOR_DB_BACKUP_DATABASE") or os.getenv("PGDATABASE") or "unifiedcollector"


def _default_docker_exe() -> str:
    configured = os.getenv("DOCKER_EXE") or os.getenv("DOCKER")
    if configured:
        return configured
    windows_default = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
    if windows_default.exists():
        return str(windows_default)
    return "docker"


def _run_pg_dump(tmp: Path, *, pg_dump_exe: str, database: str) -> None:
    cmd = [pg_dump_exe, "-Fc", "-f", str(tmp)]
    dsn = os.getenv("DATABASE_URL")
    # In Docker, ../.env may still contain a host-facing DATABASE_URL such as
    # localhost:5500. If PGHOST is explicitly set, trust libpq env instead.
    cmd.append(dsn if dsn and not os.getenv("PGHOST") else database)
    _run(cmd, "pg_dump failed")
    _ensure_nonempty(tmp)


def _validate_dump(tmp: Path, *, pg_restore_exe: str) -> None:
    _run([pg_restore_exe, "--list", str(tmp)], "pg_restore validation failed")


def _run_docker_pg_dump(
    tmp: Path,
    *,
    docker_container: str,
    docker_exe: str,
    database: str,
) -> None:
    shell = (
        'PGPASSWORD="${POSTGRES_PASSWORD:-}" '
        'pg_dump -U "${POSTGRES_USER:-collector}" -Fc '
        f"{_sh_quote(database)}"
    )
    with tmp.open("wb") as fh:
        _run(
            [docker_exe, "exec", docker_container, "sh", "-c", shell],
            "docker pg_dump failed",
            stdout=fh,
        )
    _ensure_nonempty(tmp)


def _validate_docker_dump(tmp: Path, *, docker_container: str, docker_exe: str) -> None:
    with tmp.open("rb") as fh:
        _run(
            [docker_exe, "exec", "-i", docker_container, "pg_restore", "--list"],
            "docker pg_restore validation failed",
            stdin=fh,
        )


def _ensure_nonempty(path: Path) -> None:
    if not path.exists() or path.stat().st_size <= 0:
        raise RuntimeError(f"dump file is empty: {path}")


def _run(
    cmd: Sequence[str],
    message: str,
    *,
    stdin=None,
    stdout=None,
) -> None:
    result = subprocess.run(
        list(cmd),
        stdin=stdin,
        stdout=stdout if stdout is not None else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{message}: {err or 'exit code ' + str(result.returncode)}")


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def format_plan(plan: RetentionPlan) -> str:
    lines = [
        f"keep: {len(plan.keep)}",
        f"prune: {len(plan.prune)}",
    ]
    for backup in plan.keep:
        reason = ",".join(plan.reasons.get(backup.path, ()))
        lines.append(f"KEEP  {backup.path}  [{reason}]")
    for backup in plan.prune:
        lines.append(f"PRUNE {backup.path}")
    return "\n".join(lines)


def plan_to_json(plan: RetentionPlan) -> str:
    payload = {
        "keep": [
            {
                "path": str(backup.path),
                "created_at": backup.created_at.isoformat(),
                "reasons": list(plan.reasons.get(backup.path, ())),
            }
            for backup in plan.keep
        ],
        "prune": [
            {
                "path": str(backup.path),
                "created_at": backup.created_at.isoformat(),
            }
            for backup in plan.prune
        ],
    }
    return json.dumps(payload, indent=2)


def notify_failure(message: str) -> None:
    token = os.getenv("NOTIFY_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    query = urllib.parse.urlencode({"chat_id": chat_id, "text": message})
    url = f"https://api.telegram.org/bot{token}/sendMessage?{query}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            response.read()
    except Exception as exc:
        print(f"[backup] alert send failed: {exc}", file=sys.stderr)


def run_once(args: argparse.Namespace) -> int:
    backup_dir = Path(args.backup_dir)
    policy = RetentionPolicy(args.daily, args.weekly, args.monthly)
    prefix = args.prefix

    if args.command == "list":
        plan = build_retention_plan(list_backup_files(backup_dir, prefix=prefix), policy)
        print(plan_to_json(plan) if args.json else format_plan(plan))
        return 0

    if args.command == "run":
        if args.dry_run:
            planned = create_dump(backup_dir, prefix=prefix, now=datetime.now(), dry_run=True)
            print(f"DRY RUN: would create {planned}")
        else:
            created = create_dump(
                backup_dir,
                prefix=prefix,
                database=args.database,
                pg_dump_exe=args.pg_dump,
                pg_restore_exe=args.pg_restore,
                docker_container=args.docker_container,
                docker_exe=args.docker_exe,
            )
            print(f"[backup] created {created} ({created.stat().st_size} bytes)")

    plan = build_retention_plan(list_backup_files(backup_dir, prefix=prefix), policy)
    print(plan_to_json(plan) if args.json else format_plan(plan))
    deleted = apply_retention_plan(plan, dry_run=args.dry_run or args.command != "run")
    if args.dry_run:
        print(f"DRY RUN: would prune {len(deleted)} backup(s)")
    elif args.command == "run":
        print(f"[backup] pruned {len(deleted)} backup(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dump and retain UnifiedCollector DB backups")
    parser.add_argument("command", nargs="?", choices=("run", "list"), default="run")
    parser.add_argument("--backup-dir", default=str(default_backup_dir()))
    parser.add_argument("--prefix", default=os.getenv("COLLECTOR_DB_BACKUP_PREFIX", DEFAULT_PREFIX))
    parser.add_argument("--daily", type=int, default=default_policy().daily)
    parser.add_argument("--weekly", type=int, default=default_policy().weekly)
    parser.add_argument("--monthly", type=int, default=default_policy().monthly)
    parser.add_argument("--database", default=_default_database())
    parser.add_argument("--pg-dump", default=os.getenv("PG_DUMP_EXE", "pg_dump"))
    parser.add_argument("--pg-restore", default=os.getenv("PG_RESTORE_EXE", "pg_restore"))
    parser.add_argument("--docker-container", default=os.getenv("COLLECTOR_DB_BACKUP_DOCKER_CONTAINER"))
    parser.add_argument("--docker-exe", default=os.getenv("DOCKER_EXE") or os.getenv("DOCKER"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print retention plan as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_once(args)
    except Exception as exc:
        ts = time.strftime(TIMESTAMP_FORMAT)
        print(f"[backup] FAILED {ts}: {exc}", file=sys.stderr)
        notify_failure(f"UnifiedCollector DB backup FAILED {ts}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
