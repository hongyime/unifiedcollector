from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path

from src.db.connection import close_pool, get_pool
from src.db.migrate import apply_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)


def _load_recon_spiderfoot():
    module_path = Path(__file__).resolve().parent / "core" / "recon_spiderfoot.py"
    spec = importlib.util.spec_from_file_location("uc_recon_spiderfoot", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load recon_spiderfoot module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(args) -> None:
    recon_spiderfoot = _load_recon_spiderfoot()
    pool = await get_pool()
    await apply_all(pool)
    try:
        while True:
            async with pool.acquire() as conn:
                report = await recon_spiderfoot.run_spiderfoot_once(conn, dry_run=args.dry_run)
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True, default=str), flush=True)
            else:
                print(report, flush=True)
            if args.once:
                break
            await asyncio.sleep(args.poll_interval)
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="UnifiedCollector SpiderFoot sidecar")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=60.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
