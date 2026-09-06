"""``schedule`` and ``target`` commands.

Preserves the historical positional call signatures for ``_cmd_schedule`` and
``_cmd_target``.
"""
from __future__ import annotations

from src.db.connection import close_pool, get_pool


async def _cmd_schedule(source: str, interval: int):
    from src.main import init_db
    from src.scheduler import Scheduler

    pool = await get_pool()
    await init_db(pool)
    sched = Scheduler()
    sched.pool = pool
    await sched.add_schedule(source, interval)
    print(f"Scheduled {source} every {interval}h")
    await close_pool()


async def _cmd_target(source: str, target_id: str, name: str | None, priority: int):
    from src.main import init_db

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


def _handle_schedule(args):
    return _cmd_schedule(args.source, args.interval)


def _handle_target(args):
    return _cmd_target(args.source, args.target_id, args.target_name, args.priority)


def register(subparsers) -> None:
    scp = subparsers.add_parser("schedule", help="Add/update a collection schedule")
    scp.add_argument("--source", required=True)
    scp.add_argument("--interval", type=int, default=24, help="Hours between runs")

    tp = subparsers.add_parser("target", help="Add a collection target")
    tp.add_argument("--source", required=True)
    tp.add_argument("--id", required=True, dest="target_id", help="Target identifier")
    tp.add_argument("--name", dest="target_name", help="Display name")
    tp.add_argument("--priority", type=int, default=0)


HANDLERS = {
    "schedule": _handle_schedule,
    "target": _handle_target,
}
