"""Run a frozen verified-exclusion purge with a durable receipt."""
from __future__ import annotations

import json

from src.db.connection import close_pool, get_pool


async def _cmd_verified_exclusion(args) -> None:
    from src.core.verified_exclusion import (
        cleanup_verified_exclusion_media,
        persist_verified_exclusion_manifest,
        purge_verified_exclusion_manifest,
    )
    from src.main import init_db

    if args.purge and not args.manifest:
        raise SystemExit("--manifest is required with --purge")
    if args.cleanup_media and not args.purge:
        raise SystemExit("--cleanup-media requires --purge")
    if args.cleanup_receipt and (args.purge or args.cleanup_media):
        raise SystemExit("--cleanup-receipt cannot be combined with --purge or --cleanup-media")
    pool = await get_pool()
    await init_db(pool)
    try:
        async with pool.acquire() as conn:
            if args.cleanup_receipt:
                report = {
                    "receipt_id": args.cleanup_receipt,
                    "media_cleanup": await cleanup_verified_exclusion_media(conn, args.cleanup_receipt),
                }
            elif not args.purge:
                report = await persist_verified_exclusion_manifest(conn)
            else:
                report = await purge_verified_exclusion_manifest(conn, args.manifest)
                if args.cleanup_media:
                    report["media_cleanup"] = await cleanup_verified_exclusion_media(
                        conn, report["receipt_id"],
                    )
    finally:
        await close_pool()
    if args.json:
        print(json.dumps(report, sort_keys=True, default=str))
    else:
        if args.purge or args.cleanup_receipt:
            print(f"verified-exclusion receipt: {report['receipt_id']}")
        else:
            print(f"verified-exclusion manifest: {report['id']}")


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "verified-exclusion",
        help="Create a verified-exclusion manifest or purge one frozen manifest",
    )
    parser.add_argument("--manifest", help="Frozen manifest UUID required with --purge")
    parser.add_argument("--purge", action="store_true", help="Execute the frozen transactional purge")
    parser.add_argument("--cleanup-media", action="store_true", help="Reconcile only unshared files after --purge")
    parser.add_argument("--cleanup-receipt", help="Resume media cleanup for a committed purge receipt")
    parser.add_argument("--json", action="store_true")


HANDLERS = {"verified-exclusion": _cmd_verified_exclusion}
