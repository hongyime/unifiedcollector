"""Recon commands: ``recon-queue``, ``recon-spiderfoot``, ``recon-seed``."""
from __future__ import annotations

import argparse
import asyncio
import json

from src.db.connection import close_pool, get_pool


# ── recon-queue ───────────────────────────────────────────────

async def _cmd_recon_queue(args):
    from src.core.recon import queue_recon_target
    from src.main import init_db

    scope = {}
    if args.allowlist:
        scope["allowlist"] = [item.strip() for item in args.allowlist.split(",") if item.strip()]
    if args.modules:
        scope["modules"] = [item.strip() for item in args.modules.split(",") if item.strip()]
    pool = await get_pool()
    await init_db(pool)
    async with pool.acquire() as conn:
        result = await queue_recon_target(
            conn,
            target_type=args.target_type,
            target_value=args.target_value,
            source=args.source,
            priority=args.priority,
            scope=scope,
        )
    await close_pool()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(f"queued {result['target_type']} {result['target_value']} ({result['status']})")


# ── recon-seed ────────────────────────────────────────────────

async def _cmd_recon_seed(args):
    from src.core.recon_seed import seed_recon_targets_from_collector
    from src.main import init_db

    sources = [item.strip() for item in args.source.split(",") if item.strip()] if args.source else None
    pool = await get_pool()
    await init_db(pool)
    async with pool.acquire() as conn:
        result = await seed_recon_targets_from_collector(
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
    await close_pool()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(
            f"recon seed: candidates={result['candidates']} "
            f"queued={result['queued']} dry_run={result['dry_run']}"
        )


# ── recon-spiderfoot ──────────────────────────────────────────

async def _cmd_recon_spiderfoot(args):
    from src.core.recon_spiderfoot import run_spiderfoot_once
    from src.main import init_db

    pool = await get_pool()
    await init_db(pool)
    try:
        while True:
            async with pool.acquire() as conn:
                report = await run_spiderfoot_once(conn, dry_run=args.dry_run)
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True, default=str))
            else:
                print(report)
            if args.once:
                break
            await asyncio.sleep(args.poll_interval)
    finally:
        await close_pool()


# ── argparse registration ─────────────────────────────────────

def register(subparsers) -> None:
    rq = subparsers.add_parser("recon-queue", help="Queue a bounded recon target")
    rq.add_argument("--type", required=True, dest="target_type", choices=["domain", "ip", "email", "username", "url", "phone"])
    rq.add_argument("--value", required=True, dest="target_value")
    rq.add_argument("--source", default="manual")
    rq.add_argument("--priority", type=int, default=5)
    rq.add_argument("--allowlist", default=None, help="Comma-separated allowed scope for this target")
    rq.add_argument("--modules", default=None, help="Comma-separated SpiderFoot modules for this target")
    rq.add_argument("--json", action="store_true")

    rs = subparsers.add_parser("recon-spiderfoot", help="Run guarded SpiderFoot recon sidecar")
    rs.add_argument("--once", action="store_true")
    rs.add_argument("--dry-run", action="store_true")
    rs.add_argument("--poll-interval", type=float, default=60.0)
    rs.add_argument("--json", action="store_true")

    rseed = subparsers.add_parser("recon-seed", help="Queue collector-derived recon targets")
    rseed.add_argument("--source", default=None, help="Comma-separated collector sources/platforms")
    rseed.add_argument("--no-domains", action="store_true")
    rseed.add_argument("--include-urls", action="store_true", help="Opt in to raw URL targets; paths may contain secrets")
    rseed.add_argument("--no-urls", action="store_true", help=argparse.SUPPRESS)
    rseed.add_argument("--no-usernames", action="store_true")
    rseed.add_argument("--per-source-limit", type=int, default=25)
    rseed.add_argument("--limit", type=int, default=200)
    rseed.add_argument("--priority", type=int, default=7)
    rseed.add_argument("--dry-run", action="store_true")
    rseed.add_argument("--json", action="store_true")


HANDLERS = {
    "recon-queue": _cmd_recon_queue,
    "recon-spiderfoot": _cmd_recon_spiderfoot,
    "recon-seed": _cmd_recon_seed,
}
