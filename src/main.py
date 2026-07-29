import argparse
import asyncio
import logging
import sys

from src.core.drive_check import check_drive
from src.db.connection import get_pool, close_pool

# NOTE: P2-3 attempted a QueueHandler/QueueListener pipeline here but it
# deadlocked the collector (main thread stuck in futex_wait on the logging
# lock; 0% CPU freeze ~2 min after start). Reverted to the known-good simple
# config. The real original freeze mitigation is silencing chatty httpx/telethon
# INFO logging (below), which is preserved.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
# Keep verbose third-party loggers from flooding the event-loop log path.
for _noisy in ("httpx", "httpcore", "telethon", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("unifiedcollector")


def _route_logs_to_stderr_for_json() -> None:
    """Keep stdout machine-readable for JSON CLI commands."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) is sys.stdout:
            handler.setStream(sys.stderr)


async def init_db(pool):
    # P0-1/P0-2: single DDL authority. Applies base schemas/ then pending
    # migrations/ via the ledger-backed runner. The old code globbed schemas/
    # ONLY, silently omitting 19 live, code-referenced tables under migrations/.
    from src.db.migrate import apply_all
    from src.core.maintenance import run_collector_maintenance
    await apply_all(pool)
    await run_collector_maintenance(pool)


def main():
    parser = argparse.ArgumentParser(description="UnifiedCollector")
    sub = parser.add_subparsers(dest="command")

    # worker
    wp = sub.add_parser("worker", help="Run collector worker(s)")
    wp.add_argument("--source", help="Single source to run")
    wp.add_argument("--targets", help="Comma-separated targets (for single source)")
    wp.add_argument("--all", dest="all_sources", action="store_true", help="Run all sources")

    # scheduler
    sub.add_parser("scheduler", help="Run the schedule service")

    # run  (worker + scheduler together)
    sub.add_parser("run", help="Run worker + scheduler together")

    # list
    sub.add_parser("list", help="List available sources")

    # status
    sp = sub.add_parser("status", help="Show collection status")
    sp.add_argument("--source", help="Filter by source")

    # rebuild-report
    rp = sub.add_parser("rebuild-report", help="Dry-run rebuild coverage from vault sidecars")
    rp.add_argument("--vault-root", default=None, help="Vault root (default: COLLECTOR_VAULT_ROOT)")
    rp.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    rp.add_argument("--verify-checksums", action="store_true", help="Hash referenced files while scanning")
    rp.add_argument("--compare-db", action="store_true", help="Also compare vault artifacts against media_items")
    rp.add_argument("--compare-db-limit", type=int, default=None, help="Limit media_items rows for a quick sample")
    rp.add_argument("--sidecar-limit", type=int, default=None, help="Limit sidecars scanned for a quick sample")
    rp.add_argument("--blob-limit", type=int, default=None, help="Limit canonical blob files scanned for a quick sample")

    # rebuild-rehearsal
    rr = sub.add_parser(
        "rebuild-rehearsal",
        help="Materialize media and raw-payload sidecars into a scratch SQLite DB",
    )
    rr.add_argument("--vault-root", default=None, help="Vault root (default: COLLECTOR_VAULT_ROOT)")
    rr.add_argument("--scratch-db", default=None, help="Scratch SQLite path (default: in-memory)")
    rr.add_argument("--sidecar-limit", type=int, default=None, help="Limit media sidecars scanned for a quick sample")
    rr.add_argument("--raw-payload-limit", type=int, default=None, help="Limit raw-payload sidecars scanned (defaults to --sidecar-limit)")
    rr.add_argument("--no-verify-files", action="store_true", help="Skip file existence/size checks")
    rr.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    # vault-inspect
    vi = sub.add_parser(
        "vault-inspect",
        help="Inspect vault sidecars and file/raw references without DB access",
    )
    vi.add_argument("--vault-root", default=None, help="Vault root (default: COLLECTOR_VAULT_ROOT)")
    vi.add_argument("--source", default=None, help="Optional source filter")
    vi.add_argument("--limit", type=int, default=20, help="Maximum artifacts to return")
    vi.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    # repair-media-sidecars
    msr = sub.add_parser(
        "repair-media-sidecars",
        help="Repair media_items rows that have files but lack occurrence sidecar metadata",
    )
    msr.add_argument("--source", default=None, help="Optional source filter")
    msr.add_argument("--limit", type=int, default=500, help="Maximum rows to scan")
    msr.add_argument("--since-hours", type=int, default=None, help="Only inspect rows collected in this window")
    msr.add_argument("--vault-root", default=None, help="Vault root (default: COLLECTOR_VAULT_ROOT)")
    msr.add_argument("--dry-run", action="store_true", help="Report repairable rows without writing sidecars")
    msr.add_argument(
        "--partial-artifacts",
        action="store_true",
        help="Repair media rows whose canonical vault artifact sidecar previously failed",
    )
    msr.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    # media-artifact-audit
    maa = sub.add_parser(
        "media-artifact-audit",
        help="Read-only bounded audit of DB media rows, local files, and sidecar files",
    )
    maa.add_argument("--source", default=None, help="Optional source filter")
    maa.add_argument("--sample-per-source", type=int, default=100, help="Rows to sample per source")
    maa.add_argument("--cursor-after", default="", help="Start after this content_id for keyset paging")
    maa.add_argument("--timeout", type=float, default=5.0, help="DB timeout per source query in seconds")
    maa.add_argument("--vault-root", default=None, help="Vault root (default: COLLECTOR_VAULT_ROOT)")
    maa.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    # backfill-discovered-links
    dlb = sub.add_parser(
        "backfill-discovered-links",
        help="Backfill generic discovered_links from historical source text",
    )
    dlb.add_argument("--source", default="all", choices=["all", "youtube", "telegram"])
    dlb.add_argument("--limit", type=int, default=100, help="Maximum rows per source for this run")
    dlb.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    # restore-drill
    rd = sub.add_parser(
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

    # schedule
    scp = sub.add_parser("schedule", help="Add/update a collection schedule")
    scp.add_argument("--source", required=True)
    scp.add_argument("--interval", type=int, default=24, help="Hours between runs")

    # target
    tp = sub.add_parser("target", help="Add a collection target")
    tp.add_argument("--source", required=True)
    tp.add_argument("--id", required=True, dest="target_id", help="Target identifier")
    tp.add_argument("--name", dest="target_name", help="Display name")
    tp.add_argument("--priority", type=int, default=0)

    args = parser.parse_args()
    if getattr(args, "json", False):
        _route_logs_to_stderr_for_json()

    if args.command == "worker":
        asyncio.run(_cmd_worker(args))
    elif args.command == "scheduler":
        asyncio.run(_cmd_scheduler())
    elif args.command == "run":
        asyncio.run(_cmd_run())
    elif args.command == "list":
        _cmd_list()
    elif args.command == "status":
        asyncio.run(_cmd_status(getattr(args, "source", None)))
    elif args.command == "rebuild-report":
        asyncio.run(_cmd_rebuild_report(
            args.vault_root,
            args.json,
            args.verify_checksums,
            args.compare_db,
            args.compare_db_limit,
            args.sidecar_limit,
            args.blob_limit,
        ))
    elif args.command == "rebuild-rehearsal":
        _cmd_rebuild_rehearsal(
            args.vault_root,
            args.scratch_db,
            args.sidecar_limit,
            args.raw_payload_limit,
            not args.no_verify_files,
            args.json,
        )
    elif args.command == "vault-inspect":
        _cmd_vault_inspect(args.vault_root, args.source, args.limit, args.json)
    elif args.command == "repair-media-sidecars":
        asyncio.run(_cmd_repair_media_sidecars(args))
    elif args.command == "media-artifact-audit":
        asyncio.run(_cmd_media_artifact_audit(args))
    elif args.command == "backfill-discovered-links":
        asyncio.run(_cmd_backfill_discovered_links(args))
    elif args.command == "restore-drill":
        asyncio.run(_cmd_restore_drill(args))
    elif args.command == "schedule":
        asyncio.run(_cmd_schedule(args.source, args.interval))
    elif args.command == "target":
        asyncio.run(_cmd_target(args.source, args.target_id, args.target_name, args.priority))
    else:
        parser.print_help()


# ── worker ────────────────────────────────────────────────────

async def _cmd_worker(args):
    if args.all_sources:
        from src.collectors import list_sources
        from src.worker import run_worker
        await run_worker(list_sources())
    elif args.source and args.targets:
        from src.collectors import get_collector
        if not check_drive():
            logger.error("Drive not available"); sys.exit(1)
        pool = await get_pool()
        await init_db(pool)
        collector = get_collector(args.source)
        collector.set_pool(pool)
        targets = [t.strip() for t in args.targets.split(",") if t.strip()]
        try:
            await collector.run(targets)
        finally:
            await close_pool()
    elif args.source:
        from src.worker import run_worker
        # Accept a comma-separated list so several low-risk/low-volume sources
        # can share ONE worker process (e.g. --source github,strava,search),
        # saving a Python-interpreter RSS baseline per merged source.
        sources = [s.strip() for s in args.source.split(",") if s.strip()]
        await run_worker(sources)
    else:
        print("Specify --all or --source (optionally with --targets)")


# ── scheduler ─────────────────────────────────────────────────

async def _cmd_scheduler():
    from src.scheduler import run_scheduler
    await run_scheduler()


# ── run (combined) ────────────────────────────────────────────

async def _cmd_run():
    from src.collectors import list_sources
    from src.worker import run_worker
    from src.scheduler import run_scheduler
    await asyncio.gather(
        run_worker(list_sources()),
        run_scheduler(),
    )


# ── list ──────────────────────────────────────────────────────

def _cmd_list():
    from src.collectors import list_sources
    print("Available sources:")
    for s in list_sources():
        print(f"  - {s}")


# ── status ────────────────────────────────────────────────────

async def _cmd_status(source: str | None):
    pool = await get_pool()
    await init_db(pool)
    async with pool.acquire() as conn:
        if source:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM media_items WHERE source = $1", source,
            )
            cursor = await conn.fetchrow(
                "SELECT * FROM service_cursors WHERE service = $1", source,
            )
            print(f"{source}: {count} items")
            if cursor:
                print(f"  status: {cursor['status']}, last: {cursor['last_processed_id']}")
        else:
            rows = await conn.fetch(
                "SELECT source, COUNT(*) AS count, COALESCE(SUM(file_size),0) AS bytes "
                "FROM media_items GROUP BY source ORDER BY source"
            )
            if rows:
                for r in rows:
                    gb = r["bytes"] / (1024**3)
                    print(f"  {r['source']}: {r['count']} items ({gb:.2f} GB)")
            else:
                print("  No media collected yet.")

            cursors = await conn.fetch("SELECT * FROM service_cursors ORDER BY service")
            if cursors:
                print("\nCollector status:")
                for c in cursors:
                    print(f"  {c['service']}: {c['status']}")

            schedules = await conn.fetch("SELECT * FROM collection_schedules ORDER BY source")
            if schedules:
                print("\nSchedules:")
                for s in schedules:
                    en = "enabled" if s["enabled"] else "disabled"
                    print(f"  {s['source']}: every {s['interval_hours']}h ({en}), next: {s['next_run']}")
    await close_pool()


async def _cmd_rebuild_report(
    vault_root: str | None,
    as_json: bool,
    verify_checksums: bool = False,
    compare_db: bool = False,
    compare_db_limit: int | None = None,
    sidecar_limit: int | None = None,
    blob_limit: int | None = None,
):
    import json

    from src.core.rebuild_report import (
        db_compare_timeout_seconds,
        scan_sidecars,
    )

    report = scan_sidecars(vault_root, verify_checksums=verify_checksums, sidecar_limit=sidecar_limit)
    if compare_db:
        timeout = db_compare_timeout_seconds()
        try:
            await asyncio.wait_for(
                _attach_rebuild_report_db_comparison(
                    report,
                    vault_root,
                    verify_checksums,
                    compare_db_limit,
                    sidecar_limit,
                    blob_limit,
                    timeout,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            report.db_comparison_enabled = True
            report.db_compare_error = "compare_db_timeout"
            report.db_compare_timeout_seconds = timeout
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(report.to_text())


async def _attach_rebuild_report_db_comparison(
    report,
    vault_root: str | None,
    verify_checksums: bool,
    compare_db_limit: int | None,
    sidecar_limit: int | None,
    blob_limit: int | None,
    timeout: float,
):
    from src.core.rebuild_report import compare_db_media_artifacts

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await compare_db_media_artifacts(
                report,
                conn,
                vault_root,
                verify_checksums=verify_checksums,
                limit=compare_db_limit,
                sidecar_limit=sidecar_limit,
                blob_limit=blob_limit,
                db_fetch_timeout=timeout,
            )
    finally:
        await close_pool()


def _cmd_rebuild_rehearsal(
    vault_root: str | None,
    scratch_db: str | None,
    sidecar_limit: int | None,
    raw_payload_limit: int | None,
    verify_files: bool,
    as_json: bool,
):
    import json

    from src.core.rebuild_rehearsal import rehearse_media_items_rebuild

    report = rehearse_media_items_rebuild(
        vault_root,
        scratch_db=scratch_db,
        sidecar_limit=sidecar_limit,
        raw_payload_limit=raw_payload_limit,
        verify_files=verify_files,
    )
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(report.to_text())


def _cmd_vault_inspect(
    vault_root: str | None,
    source: str | None,
    limit: int,
    as_json: bool,
):
    import json

    from src.core.vault_inspect import inspect_vault

    report = inspect_vault(vault_root, source=source, limit=limit)
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(report.to_text())


async def _cmd_repair_media_sidecars(args):
    import json

    from src.core.media_sidecar_repair import repair_missing_media_sidecars, repair_partial_vault_artifacts

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            if args.partial_artifacts:
                report = await repair_partial_vault_artifacts(
                    conn,
                    source=args.source,
                    limit=args.limit,
                    vault_root=args.vault_root,
                    dry_run=args.dry_run,
                )
            else:
                report = await repair_missing_media_sidecars(
                    conn,
                    source=args.source,
                    limit=args.limit,
                    since_hours=args.since_hours,
                    vault_root=args.vault_root,
                    dry_run=args.dry_run,
                )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
        else:
            print(
                "Media sidecar repair: "
                f"scanned={report.scanned} repaired={report.repaired} "
                f"failed={report.failed} skipped={report.skipped}"
            )
            for failure in report.failures[:10]:
                print(f"  failed {failure.get('source')}/{failure.get('content_id')}: {failure.get('error')}")
    finally:
        await close_pool()


async def _cmd_media_artifact_audit(args):
    import json

    from src.core.media_artifact_audit import audit_media_artifacts

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            report = await audit_media_artifacts(
                conn,
                source=args.source,
                sample_per_source=args.sample_per_source,
                cursor_after=args.cursor_after,
                timeout=args.timeout,
                vault_root=args.vault_root,
            )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
        else:
            print(
                "Media artifact audit: "
                f"mode={report.mode} sampled={report.total_sampled} "
                f"issues={report.total_issues} vault={report.vault_root}"
            )
            if report.source_error:
                print(f"  source listing failed: {report.source_error}")
            for source_report in report.sources:
                parts = [
                    f"{source_report.source}: sampled={source_report.sampled}",
                    f"total={source_report.total_media_items}",
                    f"issues={source_report.issue_count}",
                    f"file_missing={source_report.files_missing}",
                    f"size_mismatch={source_report.size_mismatches}",
                    f"sidecar_meta_missing={source_report.sidecar_metadata_missing}",
                    f"sidecar_file_missing={source_report.sidecar_files_missing}",
                    f"next_cursor={source_report.next_cursor}",
                ]
                if source_report.query_error:
                    parts.append(f"query_error={source_report.query_error}")
                print("  " + " ".join(parts))
                for failure in source_report.failures[:5]:
                    print(
                        "    "
                        f"{failure.get('kind')} {failure.get('content_id')}: "
                        f"{failure.get('detail') or failure.get('path')}"
                    )
    finally:
        await close_pool()


async def _cmd_backfill_discovered_links(args):
    import json

    from src.core.discovered_links_backfill import backfill_discovered_links

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            results = await backfill_discovered_links(
                conn,
                source=args.source,
                limit=args.limit,
            )
        if args.json:
            print(json.dumps([r.to_dict() for r in results], indent=2, sort_keys=True, default=str))
        else:
            for r in results:
                print(
                    f"{r.source}: scanned={r.scanned} links_written={r.links_written} "
                    f"last={r.last_processed_id} has_more={r.has_more}"
                )
    finally:
        await close_pool()


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


# ── schedule ──────────────────────────────────────────────────

async def _cmd_schedule(source: str, interval: int):
    from src.scheduler import Scheduler
    pool = await get_pool()
    await init_db(pool)
    sched = Scheduler()
    sched.pool = pool
    await sched.add_schedule(source, interval)
    print(f"Scheduled {source} every {interval}h")
    await close_pool()


# ── target ────────────────────────────────────────────────────

async def _cmd_target(source: str, target_id: str, name: str | None, priority: int):
    pool = await get_pool()
    await init_db(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO collection_targets (source, target_id, target_name, priority) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (source, target_id) DO UPDATE "
            "SET target_name = COALESCE($3, collection_targets.target_name), priority = $4",
            source, target_id, name, priority,
        )
    print(f"Added target {target_id} for {source}")
    await close_pool()


if __name__ == "__main__":
    main()
