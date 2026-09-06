"""``realtime-media-backfill`` command."""
from __future__ import annotations

import json
import logging
import sys

from src.db.connection import close_pool, get_pool

logger = logging.getLogger("unifiedcollector")


async def _cmd_realtime_media_backfill(args):
    from src.main import init_db
    from src.notifications.realtime_backfill import parse_sources, run_realtime_media_backfill

    try:
        sources = parse_sources(args.source, include_private=args.include_private)
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(2)
    pool = await get_pool()
    await init_db(pool)
    try:
        async with pool.acquire() as conn:
            report = await run_realtime_media_backfill(
                conn,
                sources=sources,
                since_hours=args.since_hours,
                limit=args.limit,
                per_source_limit=args.per_source_limit,
                include_profiles=args.include_profiles,
                include_existing=args.include_existing,
                include_private=args.include_private,
                dry_run=args.dry_run,
                sleep_seconds=args.sleep_seconds,
            )
    finally:
        await close_pool()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(
            "realtime media backfill: "
            f"selected={report['selected']} enqueued={report['enqueued']} "
            f"skipped={report['skipped']} stored_only={report['stored_only']} "
            f"dry_run={report['dry_run']}"
        )


def register(subparsers) -> None:
    rmb = subparsers.add_parser(
        "realtime-media-backfill",
        help="Replay a bounded set of stored media rows into the realtime Telegram feed",
    )
    rmb.add_argument("--source", required=True, help="Comma-separated explicit sources")
    rmb.add_argument("--since-hours", type=int, default=36)
    rmb.add_argument("--limit", type=int, default=12)
    rmb.add_argument("--per-source-limit", type=int, default=4)
    rmb.add_argument("--sleep-seconds", type=float, default=1.0)
    rmb.add_argument("--include-profiles", action="store_true")
    rmb.add_argument("--include-existing", action="store_true")
    rmb.add_argument("--include-private", action="store_true")
    rmb.add_argument("--dry-run", action="store_true")
    rmb.add_argument("--json", action="store_true")


HANDLERS = {"realtime-media-backfill": _cmd_realtime_media_backfill}
