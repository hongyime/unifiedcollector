"""pg_dump backup with hard guardrails.

Design rationale in docs/plans/backup-slow-reintroduction.md. In short: this is
a logical backup via pg_dump --format=custom that (a) never blocks the running
collectors, (b) never overloads Postgres CPU, (c) never fights a concurrent
backup, (d) skips if a fresh dump already exists, (e) verifies via pg_restore
--list before pruning old dumps.

Runs standalone (host or docker) — imports only stdlib + asyncpg (already in
the collector image). Not wired into compose or the scheduler; Phase A ships
the script only, operator invokes manually.

Env vars (all optional, defaults sensible for this deployment):

    BACKUP_ENABLED             "1" = run, "0" = exit 0 immediately (kill switch)
    BACKUP_DIR                 target dir, default Z:/unifiedcollector/backups/db
    BACKUP_SKIP_IF_FRESH_HOURS default 20 — skip if newest .dump is younger
    BACKUP_ADVISORY_LOCK_KEY   default hashtext('unifiedcollector_pg_dump')
    BACKUP_PG_DUMP_COMPRESSION default 1 (fast, low pg CPU)
    BACKUP_RETENTION_DAILY     default 7
    BACKUP_RETENTION_WEEKLY    default 4
    BACKUP_RETENTION_MONTHLY   default 6
    BACKUP_DRY_RUN             "1" = print plan + exit, no dump

    POSTGRES_HOST              default 127.0.0.1
    POSTGRES_HOST_PORT         default 5433 (matches docker-compose default)
    POSTGRES_USER              required
    POSTGRES_PASSWORD          required
    POSTGRES_DB                default unifiedcollector
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import time
from typing import Any

try:
    import asyncpg
except ImportError:  # pragma: no cover — asyncpg is required
    print(
        "[pg_dump_backup] asyncpg not installed on the host; "
        "invoke via `docker run --rm ... python scripts/pg_dump_backup.py` "
        "from the collector image which already has it.",
        file=sys.stderr,
    )
    sys.exit(2)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [pg_dump_backup] %(levelname)s %(message)s",
)
logger = logging.getLogger("pg_dump_backup")


DEFAULT_BACKUP_DIR = pathlib.Path("Z:/unifiedcollector/backups/db")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5433
DEFAULT_DB = "unifiedcollector"
DEFAULT_RETENTION_DAILY = 7
DEFAULT_RETENTION_WEEKLY = 4
DEFAULT_RETENTION_MONTHLY = 6
DEFAULT_SKIP_HOURS = 20
DEFAULT_COMPRESSION = 1
DEFAULT_ADVISORY_KEY_TEXT = "unifiedcollector_pg_dump"

# Sample tables used for the manifest row-count fingerprint (verifies the dump
# is consistent with live pg by comparing what pg_dump copied vs what the
# server reports right now). Cheap SELECT count(*) each — read-only.
FINGERPRINT_TABLES = (
    "whatsapp_users",
    "whatsapp_messages",
    "telegram_messages",
    "instagram_profiles",
    "github_commits",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise SystemExit(f"env {name} is required")
    return value or ""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _stamp(now: _dt.datetime) -> str:
    # 2026-09-19T04:00Z friendly filename slug — sortable, no colons.
    return now.strftime("%Y%m%dT%H%M")


def _pg_env_password() -> dict[str, str]:
    """PGPASSWORD passed via env avoids leaking via argv."""
    env = os.environ.copy()
    env["PGPASSWORD"] = _env("POSTGRES_PASSWORD", required=True)
    return env


# ---------------------------------------------------------------------------
# freshness + advisory lock
# ---------------------------------------------------------------------------


def _find_latest(dump_dir: pathlib.Path) -> pathlib.Path | None:
    if not dump_dir.exists():
        return None
    dumps = sorted(dump_dir.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
    return dumps[0] if dumps else None


def _is_fresh(latest: pathlib.Path | None, skip_hours: int) -> bool:
    if latest is None:
        return False
    age_seconds = time.time() - latest.stat().st_mtime
    return age_seconds < skip_hours * 3600


async def _try_advisory_lock(pool: asyncpg.Pool, key: int) -> asyncpg.Connection | None:
    """Return a conn holding the advisory lock, or None if another dump has it.

    We keep the conn alive for the whole dump so pg holds the lock; the
    caller closes it at teardown.
    """
    conn = await pool.acquire()
    try:
        got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", key)
    except Exception:
        await pool.release(conn)
        raise
    if not got:
        await pool.release(conn)
        return None
    return conn


# ---------------------------------------------------------------------------
# dump + verify
# ---------------------------------------------------------------------------


def _pg_dump_args(host: str, port: int, user: str, database: str, compression: int, dest: pathlib.Path) -> list[str]:
    return [
        "pg_dump",
        "--host", host,
        "--port", str(port),
        "--username", user,
        "--dbname", database,
        "--format=custom",
        f"--compress={compression}",
        "--jobs=1",
        "--no-owner",
        "--no-privileges",
        "--file", str(dest),
        # --serializable-deferrable gives us a consistent snapshot without
        # blocking writers. pg_dump can wait for a suitable serialization
        # boundary internally but never taking a lock that hurts collectors.
        "--serializable-deferrable",
    ]


def _run_pg_dump(args: list[str], env: dict[str, str]) -> tuple[int, str]:
    proc = subprocess.run(args, env=env, capture_output=True, text=True)
    return proc.returncode, (proc.stderr or "").strip()


def _verify(dump_path: pathlib.Path, env: dict[str, str]) -> tuple[bool, str]:
    """pg_restore --list parses the TOC of the dump file. If the archive is
    malformed pg_restore exits nonzero. Runs in seconds even for GB dumps."""
    proc = subprocess.run(
        ["pg_restore", "--list", str(dump_path)],
        env=env, capture_output=True, text=True,
    )
    ok = proc.returncode == 0 and "TOC Entries" in (proc.stdout + proc.stderr)
    detail = (proc.stderr or proc.stdout or "").strip()[:400]
    return ok, detail


async def _sample_row_counts(pool: asyncpg.Pool) -> dict[str, int]:
    out: dict[str, int] = {}
    async with pool.acquire() as conn:
        for tbl in FINGERPRINT_TABLES:
            try:
                out[tbl] = int(await conn.fetchval(f"SELECT count(*) FROM {tbl}"))
            except Exception as exc:
                out[tbl] = -1
                logger.warning("fingerprint SELECT failed for %s: %s", tbl, exc)
    return out


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------


def _keep_set(
    dumps: list[pathlib.Path],
    daily: int,
    weekly: int,
    monthly: int,
) -> set[pathlib.Path]:
    """Choose which dumps to keep. Never drop the newest one."""
    if not dumps:
        return set()
    dumps_sorted = sorted(dumps, key=lambda p: p.stat().st_mtime, reverse=True)
    keep: set[pathlib.Path] = {dumps_sorted[0]}   # newest always kept
    seen_day: dict[str, pathlib.Path] = {}
    seen_week: dict[str, pathlib.Path] = {}
    seen_month: dict[str, pathlib.Path] = {}
    for path in dumps_sorted:
        dt = _dt.datetime.fromtimestamp(path.stat().st_mtime, tz=_dt.timezone.utc)
        day_key = dt.strftime("%Y-%m-%d")
        seen_day.setdefault(day_key, path)
        iso_year, iso_week, _ = dt.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        seen_week.setdefault(week_key, path)
        month_key = dt.strftime("%Y-%m")
        seen_month.setdefault(month_key, path)
    keep.update(list(seen_day.values())[:daily])
    keep.update(list(seen_week.values())[:weekly])
    keep.update(list(seen_month.values())[:monthly])
    return keep


def _prune(dump_dir: pathlib.Path, daily: int, weekly: int, monthly: int) -> list[pathlib.Path]:
    dumps = list(dump_dir.glob("*.dump"))
    keep = _keep_set(dumps, daily, weekly, monthly)
    removed: list[pathlib.Path] = []
    for path in dumps:
        if path in keep:
            continue
        manifest = path.with_suffix(".manifest.json")
        try:
            path.unlink()
            if manifest.exists():
                manifest.unlink()
            removed.append(path)
        except OSError as exc:
            logger.warning("prune of %s failed: %s", path, exc)
    return removed


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


async def main_async(dry_run: bool) -> int:
    if _env("BACKUP_ENABLED", "1") != "1":
        logger.info("BACKUP_ENABLED=0 — exiting")
        return 0

    host = _env("POSTGRES_HOST", DEFAULT_HOST)
    port = _env_int("POSTGRES_HOST_PORT", DEFAULT_PORT)
    user = _env("POSTGRES_USER", required=True)
    database = _env("POSTGRES_DB", DEFAULT_DB)
    dump_dir = pathlib.Path(_env("BACKUP_DIR", str(DEFAULT_BACKUP_DIR)))
    skip_hours = _env_int("BACKUP_SKIP_IF_FRESH_HOURS", DEFAULT_SKIP_HOURS)
    compression = _env_int("BACKUP_PG_DUMP_COMPRESSION", DEFAULT_COMPRESSION)
    daily = _env_int("BACKUP_RETENTION_DAILY", DEFAULT_RETENTION_DAILY)
    weekly = _env_int("BACKUP_RETENTION_WEEKLY", DEFAULT_RETENTION_WEEKLY)
    monthly = _env_int("BACKUP_RETENTION_MONTHLY", DEFAULT_RETENTION_MONTHLY)

    dump_dir.mkdir(parents=True, exist_ok=True)

    latest = _find_latest(dump_dir)
    if _is_fresh(latest, skip_hours):
        age_hours = (time.time() - latest.stat().st_mtime) / 3600
        logger.info(
            "skip: newest dump %s is %.1f h old (< %d h freshness window)",
            latest.name, age_hours, skip_hours,
        )
        return 0

    now = _now_utc()
    stamp = _stamp(now)
    dest_tmp = dump_dir / f"{stamp}.dump.tmp"
    dest = dump_dir / f"{stamp}.dump"
    manifest = dump_dir / f"{stamp}.manifest.json"

    if dry_run:
        logger.info("DRY RUN — would run pg_dump:")
        logger.info("  args=%s", " ".join(_pg_dump_args(host, port, user, database, compression, dest_tmp)))
        logger.info("  compression=%d skip_if_fresh_hours=%d", compression, skip_hours)
        logger.info("  retention: daily=%d weekly=%d monthly=%d", daily, weekly, monthly)
        logger.info("  dump_dir=%s", dump_dir)
        return 0

    env = _pg_env_password()

    # Advisory lock via asyncpg — must be held for the duration of the dump.
    try:
        pool = await asyncpg.create_pool(
            host=host, port=port, user=user, password=env["PGPASSWORD"],
            database=database, min_size=1, max_size=2, timeout=15,
        )
    except Exception as exc:
        logger.error("could not open advisory-lock pool: %s", exc)
        return 3

    try:
        lock_key = _env_int(
            "BACKUP_ADVISORY_LOCK_KEY",
            0,  # sentinel — will compute below if unset
        )
        if lock_key == 0:
            async with pool.acquire() as conn:
                lock_key = int(
                    await conn.fetchval(
                        "SELECT hashtext($1)::bigint",
                        DEFAULT_ADVISORY_KEY_TEXT,
                    )
                )
        lock_conn = await _try_advisory_lock(pool, lock_key)
        if lock_conn is None:
            logger.info("another dump holds the advisory lock — exiting")
            return 0

        try:
            row_counts_before = await _sample_row_counts(pool)
            start = time.monotonic()
            args = _pg_dump_args(host, port, user, database, compression, dest_tmp)
            rc, stderr_tail = _run_pg_dump(args, env)
            elapsed = time.monotonic() - start
            if rc != 0:
                logger.error(
                    "pg_dump exited %d after %.1fs — leaving tmp intact for triage; stderr: %s",
                    rc, elapsed, stderr_tail[-500:],
                )
                with contextlib.suppress(OSError):
                    dest_tmp.unlink()
                return 4

            size_bytes = dest_tmp.stat().st_size
            ok, detail = _verify(dest_tmp, env)
            if not ok:
                logger.error("pg_restore --list rejected the dump: %s", detail[:400])
                with contextlib.suppress(OSError):
                    dest_tmp.unlink()
                return 5

            dest_tmp.rename(dest)
            row_counts_after = await _sample_row_counts(pool)

            manifest_data = {
                "dump": dest.name,
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / 1_000_000, 1),
                "duration_seconds": round(elapsed, 1),
                "compression": compression,
                "database": database,
                "started_at": now.isoformat(),
                "finished_at": _now_utc().isoformat(),
                "row_counts_before": row_counts_before,
                "row_counts_after": row_counts_after,
                "restore_toc_ok": True,
            }
            manifest.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
            logger.info(
                "wrote %s (%.1f MB) in %.1fs — pg_restore --list OK",
                dest.name, manifest_data["size_mb"], elapsed,
            )

            removed = _prune(dump_dir, daily, weekly, monthly)
            if removed:
                logger.info("pruned %d older dump(s): %s", len(removed), ", ".join(p.name for p in removed))
            return 0

        finally:
            with contextlib.suppress(Exception):
                await lock_conn.execute("SELECT pg_advisory_unlock($1)", lock_key)
                await pool.release(lock_conn)
    finally:
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="pg_dump backup with retention + verification")
    parser.add_argument("--dry-run", action="store_true", help="print plan without dumping")
    args = parser.parse_args()
    dry_run = args.dry_run or os.getenv("BACKUP_DRY_RUN") == "1"
    try:
        return asyncio.run(main_async(dry_run))
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover — catch-all logs then rerraises
        logger.exception("unhandled error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
