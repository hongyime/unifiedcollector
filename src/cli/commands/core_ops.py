"""Core operational commands: ``worker``, ``scheduler``, ``run``, ``list``, ``status``."""
from __future__ import annotations

import asyncio
import logging
import sys

from src.db.connection import close_pool, get_pool

logger = logging.getLogger("unifiedcollector")


# ── worker ────────────────────────────────────────────────────

async def _cmd_worker(args):
    from src.core.drive_check import check_drive

    if args.all_sources:
        from src.collectors import list_sources
        from src.worker import run_worker
        await run_worker(list_sources())
    elif args.source and args.targets:
        from src.collectors import get_collector
        from src.main import init_db
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
    from src.main import init_db

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


# ── argparse registration + dispatch ──────────────────────────

def _handle_scheduler(args):
    return _cmd_scheduler()


def _handle_run(args):
    return _cmd_run()


def _handle_list(args):
    _cmd_list()


def _handle_status(args):
    return _cmd_status(getattr(args, "source", None))


def register(subparsers) -> None:
    wp = subparsers.add_parser("worker", help="Run collector worker(s)")
    wp.add_argument("--source", help="Single source to run")
    wp.add_argument("--targets", help="Comma-separated targets (for single source)")
    wp.add_argument("--all", dest="all_sources", action="store_true", help="Run all sources")

    subparsers.add_parser("scheduler", help="Run the schedule service")

    subparsers.add_parser("run", help="Run worker + scheduler together")

    subparsers.add_parser("list", help="List available sources")

    sp = subparsers.add_parser("status", help="Show collection status")
    sp.add_argument("--source", help="Filter by source")


HANDLERS = {
    "worker": _cmd_worker,
    "scheduler": _handle_scheduler,
    "run": _handle_run,
    "list": _handle_list,
    "status": _handle_status,
}
