import argparse
import asyncio
import logging
import sys
from pathlib import Path

from src.core.drive_check import check_drive
from src.db.connection import get_pool, close_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Suppress noisy httpx request logs — they fill the Docker log pipe buffer
# and cause Python stdout writes to block, freezing the asyncio event loop.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("unifiedcollector")


async def init_db(pool):
    schema_dir = Path(__file__).parent / "db" / "schemas"
    async with pool.acquire() as conn:
        for sql_file in sorted(schema_dir.glob("*.sql")):
            await conn.execute(sql_file.read_text())
            logger.info("Applied schema: %s", sql_file.name)


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
        await run_worker([args.source])
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
