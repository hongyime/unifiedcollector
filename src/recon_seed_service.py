from __future__ import annotations

import argparse
import asyncio
import json

from src.core.recon_seed import seed_recon_targets_from_collector
from src.db.connection import close_pool, get_pool
from src.db.migrate import apply_all


async def _run(args) -> None:
    sources = [item.strip() for item in args.source.split(",") if item.strip()] if args.source else None
    pool = await get_pool()
    await apply_all(pool)
    try:
        async with pool.acquire() as conn:
            report = await seed_recon_targets_from_collector(
                conn,
                sources=sources,
                include_domains=not args.no_domains,
                include_urls=args.include_urls and not args.no_urls,
                include_usernames=not args.no_usernames,
                per_source_limit=args.per_source_limit,
                total_limit=args.limit,
                priority=args.priority,
                dry_run=args.dry_run,
            )
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True, default=str), flush=True)
        else:
            print(
                "recon-seed "
                f"dry_run={report.get('dry_run')} "
                f"candidates={report.get('candidates')} "
                f"queued={report.get('queued')} "
                f"skipped={report.get('skipped', 0)}",
                flush=True,
            )
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="Queue Collector-derived SpiderFoot recon targets")
    parser.add_argument("--source", default=None, help="Comma-separated collector sources/platforms")
    parser.add_argument("--no-domains", action="store_true")
    parser.add_argument("--include-urls", action="store_true", help="Opt in to raw URL targets; paths may contain secrets")
    parser.add_argument("--no-urls", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-usernames", action="store_true")
    parser.add_argument("--per-source-limit", type=int, default=25)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--priority", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
