"""``restore-drill`` command."""
from __future__ import annotations

import sys


async def _cmd_restore_drill(args):
    from src.backup.restore_drill import (
        RestoreDrillError,
        config_from_env,
        report_to_json,
        run_restore_drill,
        write_report,
    )

    try:
        config = config_from_env(
            backup_dir=args.backup_dir,
            backup_path=args.backup_path,
            database_url=args.database_url,
            scratch_database=args.scratch_db,
            pg_restore_bin=args.pg_restore_bin,
            docker_container=args.docker_container,
            docker_exe=args.docker_exe,
            restore_timeout_seconds=args.restore_timeout_seconds,
            keep_scratch=args.keep_scratch,
            dry_run=args.dry_run,
            rehearse_sidecars=args.rehearse_sidecars,
            vault_root=args.vault_root,
            sidecar_limit=args.sidecar_limit,
            raw_payload_limit=args.raw_payload_limit,
            verify_files=not args.no_verify_files,
        )
        report = await run_restore_drill(config)
        if args.report_path:
            write_report(report, args.report_path)
        print(report_to_json(report) if args.json else report.to_text())
        if report.error:
            sys.exit(1)
    except RestoreDrillError as exc:
        print(f"restore-drill failed: {exc}", file=sys.stderr)
        sys.exit(1)


def register(subparsers) -> None:
    rd = subparsers.add_parser(
        "restore-drill",
        help="Restore the latest collector DB dump into a scratch Postgres DB and report recovery evidence",
    )
    rd.add_argument("--backup-dir", default=None, help="Backup directory (default: COLLECTOR_DB_BACKUP_DIR / vault)")
    rd.add_argument("--backup-path", default=None, help="Specific dump path to restore")
    rd.add_argument("--database-url", default=None, help="Admin/source database URL (default: DATABASE_URL)")
    rd.add_argument("--scratch-db", default=None, help="Scratch database name (must start uc_restore_drill_)")
    rd.add_argument("--pg-restore-bin", default=None, help="pg_restore binary path/name")
    rd.add_argument("--docker-container", default=None, help="Postgres container to run psql/pg_restore in")
    rd.add_argument("--docker-exe", default=None, help="Docker executable for --docker-container mode")
    rd.add_argument("--restore-timeout-seconds", type=int, default=None, help="pg_restore timeout")
    rd.add_argument("--keep-scratch", action="store_true", help="Keep the scratch DB for manual inspection")
    rd.add_argument("--dry-run", action="store_true", help="Select backup and scratch name without restoring")
    rd.add_argument("--rehearse-sidecars", action="store_true", help="Also materialize vault sidecars into scratch SQLite")
    rd.add_argument("--vault-root", default=None, help="Vault root for sidecar rehearsal")
    rd.add_argument("--sidecar-limit", type=int, default=500, help="Sidecar rehearsal media limit")
    rd.add_argument("--raw-payload-limit", type=int, default=500, help="Sidecar rehearsal raw-payload limit")
    rd.add_argument("--no-verify-files", action="store_true", help="Skip sidecar file checksum checks")
    rd.add_argument("--report-path", default=None, help="Write JSON report to this path")
    rd.add_argument("--json", action="store_true", help="Print machine-readable JSON")


HANDLERS = {"restore-drill": _cmd_restore_drill}
