#!/usr/bin/env python3
"""Host-side optional rollout gate for Collector optional features."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


async def _run(args: argparse.Namespace) -> dict:
    from src.core.optional_rollout import apply_optional_rollout, optional_rollout_report
    from src.db.connection import close_pool, get_pool
    from src.main import init_db

    pool = await get_pool()
    await init_db(pool)
    try:
        async with pool.acquire() as conn:
            if args.apply:
                return await apply_optional_rollout(
                    conn,
                    feature=args.feature,
                    stage=args.stage,
                    window_hours=args.window_hours,
                    limit=args.limit,
                )
            return await optional_rollout_report(
                conn,
                feature=args.feature,
                stage=args.stage,
                window_hours=args.window_hours,
                limit=args.limit,
            )
    finally:
        await close_pool()


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate optional UnifiedCollector rollout stages")
    parser.add_argument("--feature", default="spiderfoot", choices=["spiderfoot", "recon", "lemon8", "browser-heavy"])
    parser.add_argument("--stage", default="dry-run", choices=["dry-run", "five", "daily25"])
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true", help="Apply the gated action for supported features")
    parser.add_argument("--no-fail-on-stop", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        applied = report.get("applied") or {}
        applied_text = ""
        if applied:
            applied_text = f" applied={applied.get('applied', False)}"
            if "queued" in applied:
                applied_text += f" queued={applied.get('queued', 0)}"
        print(
            f"optional-rollout feature={report['feature']} stage={report['stage']} "
            f"action={report['recommended_action']} can_proceed={report['can_proceed']} "
            f"cap={report['target_cap']} candidates={report['candidate_count']} "
            f"stops={len(report.get('stop_reasons') or [])}{applied_text}"
        )
    if report.get("recommended_action") == "stop_or_rollback" and not args.no_fail_on_stop:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
