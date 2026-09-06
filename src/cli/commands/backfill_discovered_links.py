"""``backfill-discovered-links`` command."""
from __future__ import annotations

import json

from src.db.connection import close_pool, get_pool


async def _cmd_backfill_discovered_links(args):
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


def register(subparsers) -> None:
    dlb = subparsers.add_parser(
        "backfill-discovered-links",
        help="Backfill generic discovered_links from historical source text",
    )
    dlb.add_argument("--source", default="all", choices=["all", "youtube", "telegram"])
    dlb.add_argument("--limit", type=int, default=100, help="Maximum rows per source for this run")
    dlb.add_argument("--json", action="store_true", help="Print machine-readable JSON")


HANDLERS = {"backfill-discovered-links": _cmd_backfill_discovered_links}
