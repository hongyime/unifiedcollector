from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import asyncpg

from src.backup.db_backup import default_backup_dir, list_backup_files
from src.core.rebuild_rehearsal import rehearse_media_items_rebuild


SCRATCH_DB_RE = re.compile(r"^uc_restore_drill_[a-z0-9_]{8,80}$")
DEFAULT_RESTORE_TIMEOUT_SECONDS = int(os.getenv("COLLECTOR_RESTORE_DRILL_TIMEOUT_SECONDS", "21600"))


class RestoreDrillError(RuntimeError):
    """Raised when the collector restore drill cannot safely continue."""


@dataclass(frozen=True)
class RestoreDrillConfig:
    database_url: str
    backup_dir: Path
    backup_path: Path | None = None
    scratch_database: str | None = None
    pg_restore_bin: str = "pg_restore"
    docker_container: str | None = None
    docker_exe: str | None = None
    restore_timeout_seconds: int = DEFAULT_RESTORE_TIMEOUT_SECONDS
    keep_scratch: bool = False
    dry_run: bool = False
    rehearse_sidecars: bool = False
    vault_root: Path | None = None
    sidecar_limit: int | None = None
    raw_payload_limit: int | None = None
    verify_files: bool = True


@dataclass
class RestoreDrillReport:
    backup_path: str | None = None
    scratch_database: str | None = None
    dry_run: bool = False
    restored: bool = False
    restore_seconds: float | None = None
    dropped_scratch: bool = False
    kept_scratch: bool = False
    error: str | None = None
    table_counts: dict[str, int | None] = field(default_factory=dict)
    sidecar_rehearsal: dict[str, Any] | None = None
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup_path": self.backup_path,
            "scratch_database": self.scratch_database,
            "dry_run": self.dry_run,
            "restored": self.restored,
            "restore_seconds": self.restore_seconds,
            "dropped_scratch": self.dropped_scratch,
            "kept_scratch": self.kept_scratch,
            "error": self.error,
            "table_counts": self.table_counts,
            "sidecar_rehearsal": self.sidecar_rehearsal,
            "gaps": self.gaps,
        }

    def to_text(self) -> str:
        lines = [
            "Collector restore drill",
            f"Backup: {self.backup_path or 'none'}",
            f"Scratch DB: {self.scratch_database or 'none'}",
            f"Dry run: {'yes' if self.dry_run else 'no'}",
            f"Restored: {'yes' if self.restored else 'no'}",
        ]
        if self.restore_seconds is not None:
            lines.append(f"Restore time: {self.restore_seconds:.1f}s")
        if self.error:
            lines.append(f"Error: {self.error}")
        if self.table_counts:
            lines.append("")
            lines.append("Scratch table counts:")
            lines.extend(f"  {name}: {count}" for name, count in sorted(self.table_counts.items()))
        if self.sidecar_rehearsal:
            lines.append("")
            lines.append("Vault sidecar rehearsal:")
            lines.append(f"  sidecars scanned: {self.sidecar_rehearsal.get('sidecars_scanned', 0)}")
            lines.append(f"  media rows inserted: {self.sidecar_rehearsal.get('media_rows_inserted', 0)}")
            lines.append(f"  raw payload rows inserted: {self.sidecar_rehearsal.get('raw_payload_rows_inserted', 0)}")
            skipped = self.sidecar_rehearsal.get("skipped_by_reason") or {}
            if skipped:
                details = ", ".join(f"{key}={value}" for key, value in sorted(skipped.items()))
                lines.append(f"  skipped: {details}")
        if self.gaps:
            lines.append("")
            lines.append("Recovery gaps:")
            lines.extend(f"  - {gap}" for gap in self.gaps)
        if self.kept_scratch:
            lines.append("")
            lines.append("Scratch DB kept for inspection.")
        elif self.dropped_scratch:
            lines.append("")
            lines.append("Scratch DB dropped after drill.")
        return "\n".join(lines)


def default_scratch_database_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M%S")
    return f"uc_restore_drill_{stamp}"


def validate_scratch_database_name(name: str) -> str:
    value = str(name or "").strip().lower()
    if not SCRATCH_DB_RE.match(value):
        raise RestoreDrillError(
            "scratch database name must match "
            f"{SCRATCH_DB_RE.pattern}; refusing unsafe name {name!r}"
        )
    if value in {"unifiedcollector", "collector", "postgres", "template0", "template1"}:
        raise RestoreDrillError(f"refusing protected database name {name!r}")
    return value


def select_backup_path(config: RestoreDrillConfig) -> Path:
    if config.backup_path:
        path = config.backup_path
        if not path.exists() or not path.is_file():
            raise RestoreDrillError(f"backup path does not exist: {path}")
        if path.stat().st_size <= 0:
            raise RestoreDrillError(f"backup path is empty: {path}")
        return path
    backups = [item for item in list_backup_files(config.backup_dir) if item.path.stat().st_size > 0]
    if not backups:
        raise RestoreDrillError(f"no collector backup dumps found under {config.backup_dir}")
    return backups[0].path


def pg_restore_command(
    config: RestoreDrillConfig,
    backup_path: Path,
    scratch_database: str,
) -> tuple[list[str], dict[str, str]]:
    conn = _parse_database_url(config.database_url)
    restore_bin = _resolve_pg_restore(config.pg_restore_bin)
    cmd = [
        restore_bin,
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        "--dbname",
        scratch_database,
        "--host",
        str(conn["host"]),
        "--port",
        str(conn["port"]),
        "--username",
        str(conn["user"]),
        str(backup_path),
    ]
    env = os.environ.copy()
    if conn.get("password"):
        env["PGPASSWORD"] = str(conn["password"])
    if conn.get("sslmode"):
        env["PGSSLMODE"] = str(conn["sslmode"])
    return cmd, env


async def run_restore_drill(config: RestoreDrillConfig) -> RestoreDrillReport:
    backup_path = select_backup_path(config)
    scratch_database = validate_scratch_database_name(
        config.scratch_database or default_scratch_database_name()
    )
    report = RestoreDrillReport(
        backup_path=str(backup_path),
        scratch_database=scratch_database,
        dry_run=config.dry_run,
        kept_scratch=config.keep_scratch,
    )
    if config.dry_run:
        report.gaps.append("dry run only: backup was selected but not restored")
        return report

    await _create_scratch_database_for_config(config, scratch_database)
    created = True
    try:
        try:
            started = time.monotonic()
            await asyncio.to_thread(_run_pg_restore, config, backup_path, scratch_database)
            report.restore_seconds = round(time.monotonic() - started, 3)
            report.restored = True

            report.table_counts = await _table_counts_for_config(config, scratch_database)
            report.gaps.extend(_gaps_from_table_counts(report.table_counts))

            if config.rehearse_sidecars:
                sidecar_report = await asyncio.to_thread(
                    rehearse_media_items_rebuild,
                    config.vault_root,
                    sidecar_limit=config.sidecar_limit,
                    raw_payload_limit=config.raw_payload_limit,
                    verify_files=config.verify_files,
                )
                report.sidecar_rehearsal = sidecar_report.to_dict()
                report.gaps.extend(_gaps_from_sidecar_rehearsal(report.sidecar_rehearsal))
        except Exception as exc:
            report.error = str(exc)
            report.gaps.append(f"restore drill failed: {exc}")
    finally:
        if created and not config.keep_scratch:
            try:
                await _drop_scratch_database_for_config(config, scratch_database)
                report.dropped_scratch = True
            except Exception as exc:
                report.error = report.error or f"scratch drop failed: {exc}"
                report.gaps.append(f"scratch drop failed: {exc}")
        elif created:
            report.kept_scratch = True
    return report


def write_report(report: RestoreDrillReport, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report_to_json(report) + "\n", encoding="utf-8")
    return target


def report_to_json(report: RestoreDrillReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str)


def config_from_env(
    *,
    backup_dir: str | None = None,
    backup_path: str | None = None,
    database_url: str | None = None,
    scratch_database: str | None = None,
    pg_restore_bin: str | None = None,
    docker_container: str | None = None,
    docker_exe: str | None = None,
    restore_timeout_seconds: int | None = None,
    keep_scratch: bool = False,
    dry_run: bool = False,
    rehearse_sidecars: bool = False,
    vault_root: str | None = None,
    sidecar_limit: int | None = None,
    raw_payload_limit: int | None = None,
    verify_files: bool = True,
) -> RestoreDrillConfig:
    return RestoreDrillConfig(
        database_url=database_url or os.getenv("DATABASE_URL", "postgresql://collector:collector@localhost:5432/unifiedcollector"),
        backup_dir=Path(backup_dir) if backup_dir else default_backup_dir(),
        backup_path=Path(backup_path) if backup_path else None,
        scratch_database=scratch_database,
        pg_restore_bin=pg_restore_bin or os.getenv("PG_RESTORE_EXE", "pg_restore"),
        docker_container=(
            docker_container
            or os.getenv("COLLECTOR_RESTORE_DRILL_DOCKER_CONTAINER")
            or os.getenv("COLLECTOR_DB_BACKUP_DOCKER_CONTAINER")
        ),
        docker_exe=docker_exe or os.getenv("DOCKER_EXE") or os.getenv("DOCKER"),
        restore_timeout_seconds=restore_timeout_seconds or DEFAULT_RESTORE_TIMEOUT_SECONDS,
        keep_scratch=keep_scratch,
        dry_run=dry_run,
        rehearse_sidecars=rehearse_sidecars,
        vault_root=Path(vault_root) if vault_root else None,
        sidecar_limit=sidecar_limit,
        raw_payload_limit=raw_payload_limit,
        verify_files=verify_files,
    )


def _run_pg_restore(config: RestoreDrillConfig, backup_path: Path, scratch_database: str) -> None:
    if config.docker_container:
        _run_docker_pg_restore(config, backup_path, scratch_database)
        return

    cmd, env = pg_restore_command(config, backup_path, scratch_database)
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            text=True,
            capture_output=True,
            timeout=config.restore_timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RestoreDrillError(
            f"pg_restore timed out after {config.restore_timeout_seconds}s: "
            f"{_tail_text(exc.stderr or exc.stdout)}"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if _is_only_transaction_timeout_restore_warning(detail):
            return
        raise RestoreDrillError(f"pg_restore failed with exit code {proc.returncode}: {detail}")


async def _create_scratch_database(database_url: str, scratch_database: str) -> None:
    admin = await _connect_database(database_url, "postgres")
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", scratch_database)
        if exists:
            raise RestoreDrillError(f"scratch database already exists: {scratch_database}")
        await admin.execute(f'CREATE DATABASE "{scratch_database}" TEMPLATE template0')
    finally:
        await admin.close()


async def _create_scratch_database_for_config(config: RestoreDrillConfig, scratch_database: str) -> None:
    if config.docker_container:
        await asyncio.to_thread(_create_scratch_database_docker, config, scratch_database)
        return
    await _create_scratch_database(config.database_url, scratch_database)


async def _drop_scratch_database(database_url: str, scratch_database: str) -> None:
    validate_scratch_database_name(scratch_database)
    admin = await _connect_database(database_url, "postgres")
    try:
        await admin.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = $1
              AND pid <> pg_backend_pid()
            """,
            scratch_database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{scratch_database}"')
    finally:
        await admin.close()


async def _drop_scratch_database_for_config(config: RestoreDrillConfig, scratch_database: str) -> None:
    if config.docker_container:
        await asyncio.to_thread(_drop_scratch_database_docker, config, scratch_database)
        return
    await _drop_scratch_database(config.database_url, scratch_database)


async def _connect_database(database_url: str, database: str):
    conn = _parse_database_url(database_url)
    return await asyncpg.connect(
        host=conn["host"],
        port=conn["port"],
        user=conn["user"],
        password=conn["password"],
        database=database,
        ssl="disable" if str(conn.get("sslmode") or "disable") == "disable" else None,
        command_timeout=300,
    )


async def _table_counts(conn) -> dict[str, int | None]:
    tables = [
        "media_items",
        "collection_runs",
        "collection_targets",
        "rate_limit_events",
        "dead_letter_queue",
        "telegram_messages",
        "whatsapp_messages",
        "beeper_messages",
        "strava_activities",
        "strava_gps_streams",
        "instagram_posts",
        "browser_ingest_events",
    ]
    counts: dict[str, int | None] = {}
    for table in tables:
        exists = await conn.fetchval("SELECT to_regclass($1)", table)
        if exists:
            counts[table] = int(await conn.fetchval(f"SELECT count(*)::bigint FROM {table}") or 0)
        else:
            counts[table] = None
    return counts


async def _table_counts_for_config(config: RestoreDrillConfig, scratch_database: str) -> dict[str, int | None]:
    if config.docker_container:
        return await asyncio.to_thread(_table_counts_docker, config, scratch_database)
    scratch_conn = await _connect_database(config.database_url, scratch_database)
    try:
        return await _table_counts(scratch_conn)
    finally:
        await scratch_conn.close()


def _gaps_from_table_counts(table_counts: dict[str, int | None]) -> list[str]:
    gaps = []
    for name, count in sorted(table_counts.items()):
        if count is None:
            gaps.append(f"restored database is missing expected table: {name}")
    return gaps


def _gaps_from_sidecar_rehearsal(sidecar_rehearsal: dict[str, Any]) -> list[str]:
    gaps = []
    skipped = sidecar_rehearsal.get("skipped_by_reason") or {}
    for reason, count in sorted(skipped.items()):
        gaps.append(f"sidecar rehearsal skipped {count} item(s): {reason}")
    if not sidecar_rehearsal.get("media_rows_inserted") and not sidecar_rehearsal.get("raw_payload_rows_inserted"):
        gaps.append("sidecar rehearsal inserted no media or raw-payload rows")
    return gaps


def _parse_database_url(database_url: str) -> dict[str, Any]:
    parsed = urlparse(database_url)
    query = parse_qs(parsed.query)
    return {
        "host": parsed.hostname or os.getenv("PGHOST", "localhost"),
        "port": parsed.port or int(os.getenv("PGPORT", "5432")),
        "user": unquote(parsed.username or os.getenv("POSTGRES_USER", "collector")),
        "password": unquote(parsed.password or os.getenv("POSTGRES_PASSWORD", "")),
        "database": (parsed.path or "/unifiedcollector").lstrip("/") or "unifiedcollector",
        "sslmode": (query.get("sslmode") or [os.getenv("PGSSLMODE", "disable")])[0],
    }


def _resolve_pg_restore(pg_restore_bin: str) -> str:
    resolved = shutil.which(pg_restore_bin)
    if resolved:
        return resolved
    if Path(pg_restore_bin).exists():
        return pg_restore_bin
    raise RestoreDrillError(f"pg_restore binary not found: {pg_restore_bin}")


def _docker_exe(config: RestoreDrillConfig) -> str:
    if config.docker_exe:
        return config.docker_exe
    windows_default = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
    if windows_default.exists():
        return str(windows_default)
    return "docker"


def _run_docker_shell(
    config: RestoreDrillConfig,
    shell: str,
    message: str,
    *,
    stdin=None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    if not config.docker_container:
        raise RestoreDrillError("docker container is not configured")
    proc = subprocess.run(
        [_docker_exe(config), "exec", "-i", config.docker_container, "sh", "-c", shell],
        stdin=stdin,
        text=False if stdin is not None else True,
        capture_output=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace") if isinstance(proc.stderr, bytes) else proc.stderr
        stdout = proc.stdout.decode("utf-8", errors="replace") if isinstance(proc.stdout, bytes) else proc.stdout
        detail = (stderr or stdout or "").strip()
        raise RestoreDrillError(f"{message}: {detail or 'exit code ' + str(proc.returncode)}")
    return proc


def _create_scratch_database_docker(config: RestoreDrillConfig, scratch_database: str) -> None:
    validate_scratch_database_name(scratch_database)
    exists_sql = f"SELECT 1 FROM pg_database WHERE datname = '{scratch_database}'"
    exists = _run_docker_shell(
        config,
        'PGPASSWORD="${POSTGRES_PASSWORD:-}" '
        f'psql -v ON_ERROR_STOP=1 -U "${{POSTGRES_USER:-collector}}" -d postgres -tAc "{exists_sql}"',
        "docker psql scratch database existence check failed",
    )
    stdout = exists.stdout.decode("utf-8", errors="replace") if isinstance(exists.stdout, bytes) else exists.stdout
    if str(stdout or "").strip():
        raise RestoreDrillError(f"scratch database already exists: {scratch_database}")
    create_sql = f'CREATE DATABASE "{scratch_database}" TEMPLATE template0'
    _run_docker_shell(
        config,
        'PGPASSWORD="${POSTGRES_PASSWORD:-}" '
        f'psql -v ON_ERROR_STOP=1 -U "${{POSTGRES_USER:-collector}}" -d postgres -c "{create_sql}"',
        "docker psql scratch database create failed",
    )


def _drop_scratch_database_docker(config: RestoreDrillConfig, scratch_database: str) -> None:
    validate_scratch_database_name(scratch_database)
    terminate_sql = (
        "SELECT pg_terminate_backend(pid) "
        "FROM pg_stat_activity "
        f"WHERE datname = '{scratch_database}' AND pid <> pg_backend_pid()"
    )
    _run_docker_shell(
        config,
        'PGPASSWORD="${POSTGRES_PASSWORD:-}" '
        f'psql -v ON_ERROR_STOP=1 -U "${{POSTGRES_USER:-collector}}" -d postgres -c "{terminate_sql}"',
        "docker psql scratch database terminate failed",
    )
    drop_sql = f'DROP DATABASE IF EXISTS "{scratch_database}"'
    _run_docker_shell(
        config,
        'PGPASSWORD="${POSTGRES_PASSWORD:-}" '
        f'psql -v ON_ERROR_STOP=1 -U "${{POSTGRES_USER:-collector}}" -d postgres -c "{drop_sql}"',
        "docker psql scratch database drop failed",
    )


def _run_docker_pg_restore(config: RestoreDrillConfig, backup_path: Path, scratch_database: str) -> None:
    validate_scratch_database_name(scratch_database)
    shell = (
        'PGPASSWORD="${POSTGRES_PASSWORD:-}" '
        f'pg_restore --no-owner --no-privileges --exit-on-error --dbname "{scratch_database}" '
        '--username "${POSTGRES_USER:-collector}"'
    )
    try:
        with backup_path.open("rb") as fh:
            _run_docker_shell(
                config,
                shell,
                "docker pg_restore failed",
                stdin=fh,
                timeout=config.restore_timeout_seconds,
            )
    except subprocess.TimeoutExpired as exc:
        raise RestoreDrillError(
            f"docker pg_restore timed out after {config.restore_timeout_seconds}s: "
            f"{_tail_text(exc.stderr or exc.stdout)}"
        ) from exc


def _table_counts_docker(config: RestoreDrillConfig, scratch_database: str) -> dict[str, int | None]:
    validate_scratch_database_name(scratch_database)
    tables = [
        "media_items",
        "collection_runs",
        "collection_targets",
        "rate_limit_events",
        "dead_letter_queue",
        "telegram_messages",
        "whatsapp_messages",
        "beeper_messages",
        "strava_activities",
        "strava_gps_streams",
        "instagram_posts",
        "browser_ingest_events",
    ]
    counts: dict[str, int | None] = {}
    for table in tables:
        exists = _run_docker_shell(
            config,
            'PGPASSWORD="${POSTGRES_PASSWORD:-}" '
            f'psql -v ON_ERROR_STOP=1 -U "${{POSTGRES_USER:-collector}}" -d "{scratch_database}" '
            f'-tAc "SELECT to_regclass('"'public.{table}'"')"',
            f"docker psql table existence check failed for {table}",
        )
        stdout = exists.stdout.decode("utf-8", errors="replace") if isinstance(exists.stdout, bytes) else exists.stdout
        if not str(stdout or "").strip():
            counts[table] = None
            continue
        result = _run_docker_shell(
            config,
            'PGPASSWORD="${POSTGRES_PASSWORD:-}" '
            f'psql -v ON_ERROR_STOP=1 -U "${{POSTGRES_USER:-collector}}" -d "{scratch_database}" '
            f'-tAc "SELECT count(*)::bigint FROM {table}"',
            f"docker psql count failed for {table}",
        )
        value = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else result.stdout
        counts[table] = int(str(value or "0").strip() or "0")
    return counts


def _is_only_transaction_timeout_restore_warning(detail: str) -> bool:
    if 'unrecognized configuration parameter "transaction_timeout"' not in detail:
        return False
    severe = [
        line
        for line in detail.splitlines()
        if "ERROR:" in line or "FATAL:" in line
    ]
    return all('unrecognized configuration parameter "transaction_timeout"' in line for line in severe)


def _tail_text(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    return text[-limit:]
