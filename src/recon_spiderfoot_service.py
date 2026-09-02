from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
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


def format_report(report: dict) -> str:
    status = str(report.get("status") or "unknown")
    target = report.get("target") if isinstance(report.get("target"), dict) else {}
    target_type = target.get("target_type") or "none"
    observations = int(report.get("observations") or 0)
    dry_run = " dry_run" if report.get("dry_run") else ""
    modules = report.get("modules")
    module_text = ""
    if isinstance(modules, list) and modules:
        module_text = " modules=" + ",".join(str(item) for item in modules[:8])
    error = str(report.get("error") or "")[:160]
    error_text = f" error={error}" if error else ""
    return f"spiderfoot status={status} target_type={target_type} observations={observations}{module_text}{dry_run}{error_text}"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.1, value)


def _worker_count(raw: int | str | None) -> int:
    try:
        value = int(raw) if raw is not None else 1
    except (TypeError, ValueError):
        return 1
    return max(1, min(value, 8))


async def _run_once(recon_spiderfoot, pool, args, worker_id: int | str | None = None) -> dict:
    async with pool.acquire() as conn:
        report = await recon_spiderfoot.run_spiderfoot_once(conn, dry_run=args.dry_run, worker_label=worker_id)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str), flush=True)
    else:
        prefix = f"worker={worker_id} " if worker_id is not None else ""
        print(prefix + format_report(report), flush=True)
    return report


async def _worker_loop(worker_id: int, recon_spiderfoot, pool, args) -> None:
    while True:
        report = await _run_once(recon_spiderfoot, pool, args, worker_id)
        if args.once:
            break
        delay = args.poll_interval if report.get("status") == "idle" else 0.5
        await asyncio.sleep(delay)


async def _fp_blocklist_refresh_loop(recon_spiderfoot, pool) -> None:
    """Periodic maigret FP-blocklist refresh (runs in-process; has maigret bin).

    The collector scheduler container does not have the ``maigret`` binary,
    so the periodic refresh lives here — alongside the recon worker — where
    subprocess execution is available. Env-tunable:
        RECON_MAIGRET_FP_INTERVAL_SECONDS (default 604800 = 7 days)
        RECON_MAIGRET_FP_CONTROLS         (default 10)
    Serialized via pg_advisory_lock inside ``refresh_maigret_fp_blocklist`` so
    concurrent workers cannot double-run.  Idempotent — uses ON CONFLICT to
    accumulate universally-claimed sites."""
    log = logging.getLogger("recon_spiderfoot_service.fp_refresh")
    try:
        interval = max(3600, int(os.getenv("RECON_MAIGRET_FP_INTERVAL_SECONDS", "604800")))
    except ValueError:
        interval = 604800
    try:
        controls = max(1, min(50, int(os.getenv("RECON_MAIGRET_FP_CONTROLS", "10"))))
    except ValueError:
        controls = 10
    # Warm-up jitter so a batch restart doesn't dogpile all workers on refresh.
    await asyncio.sleep(30)
    while True:
        try:
            async with pool.acquire() as conn:
                await recon_spiderfoot._ensure_maigret_fp_table(conn)
                # Skip if the blocklist was refreshed within `interval` seconds.
                last = await conn.fetchval(
                    "SELECT max(last_refreshed_at) FROM recon_maigret_fp_sites"
                )
                if last is not None:
                    from datetime import datetime, timezone
                    age = (datetime.now(timezone.utc) - last).total_seconds()
                    if age < interval:
                        log.info(
                            "fp_refresh: skip (age=%ds < interval=%ds)", int(age), interval,
                        )
                        await asyncio.sleep(max(300, interval - int(age)))
                        continue
                log.info("fp_refresh: starting (controls=%d, force=False)", controls)
                summary = await recon_spiderfoot.refresh_maigret_fp_blocklist(
                    conn,
                    num_controls=controls,
                    worker_label="service-fp-refresh",
                    force=False,
                )
                log.info("fp_refresh: %s", summary)
        except Exception:
            log.exception("fp_refresh loop iteration failed")
        await asyncio.sleep(interval)



async def _run(args) -> None:
    recon_spiderfoot = _load_recon_spiderfoot()
    pool = await get_pool()
    await apply_all(pool)
    try:
        workers = _worker_count(args.workers)
        # Periodic FP-blocklist refresh runs as a sibling task — not part of the
        # target-processing worker loop — so it can't be starved by hot pending
        # queues.  Disabled with RECON_MAIGRET_FP_INTERVAL_SECONDS=0 (skips loop).
        fp_task = None
        if not args.once and os.getenv("RECON_MAIGRET_FP_INTERVAL_SECONDS", "").strip() != "0":
            fp_task = asyncio.create_task(
                _fp_blocklist_refresh_loop(recon_spiderfoot, pool),
                name="fp_blocklist_refresh",
            )
        try:
            if workers == 1:
                await _worker_loop(1, recon_spiderfoot, pool, args)
            else:
                await asyncio.gather(
                    *(_worker_loop(worker_id, recon_spiderfoot, pool, args) for worker_id in range(1, workers + 1))
                )
        finally:
            if fp_task is not None:
                fp_task.cancel()
                try:
                    await fp_task
                except (asyncio.CancelledError, Exception):
                    pass
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="UnifiedCollector SpiderFoot sidecar")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=_env_float("RECON_SPIDERFOOT_POLL_INTERVAL", 60.0))
    parser.add_argument("--workers", type=int, default=_worker_count(os.getenv("RECON_SPIDERFOOT_WORKERS", "1")))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
