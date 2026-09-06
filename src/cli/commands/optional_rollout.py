"""``optional-rollout`` command."""
from __future__ import annotations

import json

from src.db.connection import close_pool, get_pool


async def _cmd_optional_rollout(args):
    from src.core.optional_rollout import apply_optional_rollout, optional_rollout_report
    from src.main import init_db

    pool = await get_pool()
    await init_db(pool)
    async with pool.acquire() as conn:
        if args.apply:
            report = await apply_optional_rollout(
                conn,
                feature=args.feature,
                stage=args.stage,
                window_hours=args.window_hours,
                limit=args.limit,
            )
        else:
            report = await optional_rollout_report(
                conn,
                feature=args.feature,
                stage=args.stage,
                window_hours=args.window_hours,
                limit=args.limit,
            )
    await close_pool()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        action = report["recommended_action"]
        stop_count = len(report.get("stop_reasons") or [])
        applied = report.get("applied") or {}
        applied_text = ""
        if applied:
            applied_text = f" applied={applied.get('applied', False)}"
            if "queued" in applied:
                applied_text += f" queued={applied.get('queued', 0)}"
        print(
            f"optional-rollout feature={report['feature']} stage={report['stage']} "
            f"action={action} can_proceed={report['can_proceed']} "
            f"cap={report['target_cap']} candidates={report['candidate_count']} "
            f"stops={stop_count}{applied_text}"
        )
    if report.get("recommended_action") == "stop_or_rollback" and not args.no_fail_on_stop:
        raise SystemExit(2)


def register(subparsers) -> None:
    opt = subparsers.add_parser("optional-rollout", help="Gate optional collector feature rollout")
    opt.add_argument("--feature", default="spiderfoot", choices=["spiderfoot", "recon", "lemon8", "browser-heavy"])
    opt.add_argument(
        "--stage",
        default="dry-run",
        choices=["dry-run", "five", "daily25", "daily100", "daily250", "daily500"],
    )
    opt.add_argument("--window-hours", type=int, default=24)
    opt.add_argument("--limit", type=int, default=None)
    opt.add_argument("--apply", action="store_true", help="Apply the gated action for supported features")
    opt.add_argument("--no-fail-on-stop", action="store_true")
    opt.add_argument("--json", action="store_true")


HANDLERS = {"optional-rollout": _cmd_optional_rollout}
