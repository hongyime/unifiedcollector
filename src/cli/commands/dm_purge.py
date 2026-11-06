"""Run the irreversible direct-message purge after the capture shutdown deploys."""
from __future__ import annotations

import json

from src.db.connection import close_pool, get_pool


async def _cmd_purge_direct_messages(args) -> None:
    if not args.confirm:
        raise SystemExit("direct-message purge is irreversible; pass --confirm")
    from src.core.dm_purge import purge_direct_message_artifacts, purge_direct_message_rows

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                report = {"database": await purge_direct_message_rows(conn)}
        report["files"] = purge_direct_message_artifacts()
    finally:
        await close_pool()
    print(json.dumps(report, sort_keys=True))


def register(subparsers) -> None:
    parser = subparsers.add_parser("purge-direct-messages", help="Irreversibly remove retired direct-message data")
    parser.add_argument("--confirm", action="store_true", help="Required acknowledgement for irreversible deletion")


HANDLERS = {"purge-direct-messages": _cmd_purge_direct_messages}
