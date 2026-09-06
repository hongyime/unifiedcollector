"""``coverage-snapshot`` command."""
from __future__ import annotations

import json

from src.db.connection import close_pool, get_pool


async def _cmd_coverage_snapshot(args):
    from src.core.collection_coverage import build_collection_coverage_snapshot
    from src.main import init_db

    pool = await get_pool()
    await init_db(pool)
    async with pool.acquire() as conn:
        report = await build_collection_coverage_snapshot(
            conn,
            expected_cadence_hours=args.expected_cadence_hours,
            source=args.source,
            write=not args.dry_run,
        )
    await close_pool()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(report["summary"]["digest"])


def register(subparsers) -> None:
    cp = subparsers.add_parser("coverage-snapshot", help="Build collection coverage snapshot")
    cp.add_argument("--source", default=None, help="Optional source filter")
    cp.add_argument("--expected-cadence-hours", type=int, default=24)
    cp.add_argument("--dry-run", action="store_true")
    cp.add_argument("--json", action="store_true")


HANDLERS = {"coverage-snapshot": _cmd_coverage_snapshot}
