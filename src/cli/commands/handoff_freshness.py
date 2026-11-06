"""CLI completion gate for shared-agent handoff freshness."""
from __future__ import annotations

import json
from pathlib import Path

from src.db.connection import close_pool, get_pool


async def _cmd_handoff_freshness(args) -> None:
    from src.main import init_db
    from src.core.handoff_freshness import check_handoff_freshness

    pool = await get_pool()
    try:
        await init_db(pool)
        async with pool.acquire() as conn:
            report = await check_handoff_freshness(conn, state_path=Path(args.state_file))
    finally:
        await close_pool()
    print(json.dumps(report, default=str, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def register(subparsers) -> None:
    parser = subparsers.add_parser("handoff-freshness", help="Fail if STATE.md predates live operational evidence")
    parser.add_argument("--state-file", default=".agents/STATE.md")


HANDLERS = {"handoff-freshness": _cmd_handoff_freshness}
