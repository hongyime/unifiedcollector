from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path

from src.db.connection import close_pool, get_pool
from src.db.migrate import apply_all
from src.core.recon_rollout import AdmissionPolicyError, admission_snapshot

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
    error_text = " error=present" if report.get("error") else ""
    return f"spiderfoot status={status} target_type={target_type} observations={observations}{module_text}{dry_run}{error_text}"


def _redact_report(report: dict) -> dict:
    """Remove target values and error text before writing default service logs."""
    sanitized = copy.deepcopy(report)

    def visit(value):
        if isinstance(value, dict):
            for key, item in list(value.items()):
                normalized = str(key).lower()
                if normalized in {"target_value", "email", "query", "error", "reason"}:
                    value[key] = "[redacted]" if item not in (None, "") else item
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(sanitized)
    return sanitized


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


def _health_max_age_seconds() -> int:
    try:
        target_timeout = max(1, int(os.getenv("SPIDERFOOT_TARGET_TIMEOUT_SECONDS", "180")))
    except ValueError:
        target_timeout = 180
    try:
        configured = int(os.getenv("RECON_HEALTH_MAX_AGE_SECONDS", "360"))
    except ValueError:
        configured = 360
    return max(30, target_timeout + 30, configured)


def _heartbeat_path(worker_id: int | str | None = None) -> Path:
    path = Path(os.getenv(
        "RECON_SPIDERFOOT_HEARTBEAT_PATH",
        "/tmp/recon-spiderfoot-worker.heartbeat",
    ))
    return Path(f"{path}.{worker_id}") if worker_id is not None else path


def _touch_worker_heartbeat(worker_id: int | str) -> None:
    path = _heartbeat_path(worker_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _heartbeat_is_fresh(*, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    count = _worker_count(os.getenv("RECON_SPIDERFOOT_WORKERS", "1"))
    for worker_id in range(1, count + 1):
        try:
            age = current - _heartbeat_path(worker_id).stat().st_mtime
        except OSError:
            return False
        if not 0 <= age <= _health_max_age_seconds():
            return False
    return True


async def _database_is_healthy(pool) -> bool:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1") != 1:
            return False
        row = await conn.fetchrow(
            "SELECT status, updated_at FROM source_health WHERE source = $1",
            "spiderfoot",
        )
    if not row or row["status"] != "running" or row["updated_at"] is None:
        return False
    age = (time.time() - row["updated_at"].timestamp())
    return 0 <= age <= _health_max_age_seconds()


class _RunWindow:
    """Atomic per-process target and wall-clock budget shared by workers."""

    def __init__(self, max_targets: int, deadline_seconds: int):
        self.max_targets = max_targets
        self.deadline_seconds = deadline_seconds
        self.started = time.monotonic()
        self.targets = 0
        self.reserved = 0
        self.lock = asyncio.Lock()

    async def reserve(self) -> bool:
        async with self.lock:
            if time.monotonic() - self.started >= self.deadline_seconds:
                return False
            if self.targets + self.reserved >= self.max_targets:
                return False
            self.reserved += 1
            return True

    async def finish(self, found_target: bool) -> None:
        async with self.lock:
            self.reserved = max(0, self.reserved - 1)
            if found_target:
                self.targets += 1


def _run_window_config() -> tuple[int, int]:
    """Return bounded target/deadline caps; operators may lower, never unbound."""
    max_targets = min(25, max(1, int(os.getenv("RECON_MAX_TARGETS_PER_RUN", "10"))))
    deadline = min(3_600, max(30, int(os.getenv("RECON_RUN_DEADLINE_SECONDS", "1800"))))
    return max_targets, deadline


async def _check_worker_health() -> bool:
    try:
        admission_snapshot()
    except AdmissionPolicyError:
        return False
    if not _heartbeat_is_fresh():
        return False
    pool = await get_pool()
    try:
        return await _database_is_healthy(pool)
    finally:
        await close_pool()


async def _run_schema_init() -> dict:
    """Run migrations once through the explicit schema-init entry point."""
    pool = await get_pool()
    try:
        return await apply_all(pool)
    finally:
        await close_pool()


async def _run_once(recon_spiderfoot, pool, args, worker_id: int | str | None = None) -> dict:
    async with pool.acquire() as conn:
        report = await recon_spiderfoot.run_spiderfoot_once(conn, dry_run=args.dry_run, worker_label=worker_id)
    if args.json:
        print(json.dumps(_redact_report(report), indent=2, sort_keys=True, default=str), flush=True)
    else:
        prefix = f"worker={worker_id} " if worker_id is not None else ""
        print(prefix + format_report(report), flush=True)
    return report


async def _worker_loop(worker_id: int, recon_spiderfoot, pool, args, budget: _RunWindow) -> None:
    while True:
        if not await budget.reserve():
            return
        _touch_worker_heartbeat(worker_id)
        try:
            report = await _run_once(recon_spiderfoot, pool, args, worker_id)
        except BaseException:
            await budget.finish(False)
            raise
        target = report.get("target")
        await budget.finish(isinstance(target, dict) and bool(target.get("target_type")))
        _touch_worker_heartbeat(worker_id)
        if args.once:
            break
        delay = args.poll_interval if report.get("status") in {
            "idle", "admission_halted", "admission_unavailable", "guard_unavailable",
        } else 0.5
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
            policy, _state, block = admission_snapshot()
            sibling = policy["sibling_work"].get("maigret_fp_refresh")
            if block or not sibling or not sibling.get("enabled") or sibling.get("max_claims", 0) <= 0:
                log.info("fp_refresh: admission disabled or halted; skipping control work")
                await asyncio.sleep(interval)
                continue
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
    # Refuse to become a consumer unless the persistent policy and private
    # ledger are installed and internally consistent. A valid halted policy is
    # allowed; it starts idle and cannot claim or launch sibling work.
    try:
        policy, state, block = admission_snapshot()
    except AdmissionPolicyError as exc:
        raise RuntimeError("recon admission gate is unavailable; refusing startup") from exc
    logging.getLogger(__name__).info(
        "recon admission gate installed (policy=%s cohort=%s halted=%s)",
        policy["policy_id"], state["cohort_limit"], bool(block),
    )
    pool = await get_pool()
    try:
        workers = _worker_count(args.workers)
        # Periodic FP-blocklist refresh runs as a sibling task — not part of the
        # target-processing worker loop — so it can't be starved by hot pending
        # queues.  Disabled with RECON_MAIGRET_FP_INTERVAL_SECONDS=0 (skips loop).
        fp_task = None
        if not args.dry_run and not args.once and os.getenv("RECON_MAIGRET_FP_INTERVAL_SECONDS", "").strip() != "0":
            fp_task = asyncio.create_task(
                _fp_blocklist_refresh_loop(recon_spiderfoot, pool),
                name="fp_blocklist_refresh",
            )
        try:
            while True:
                max_targets, deadline = _run_window_config()
                budget = _RunWindow(max_targets, deadline)
                active_workers = 1 if args.once else workers
                await asyncio.gather(*(
                    _worker_loop(worker_id, recon_spiderfoot, pool, args, budget)
                    for worker_id in range(1, active_workers + 1)
                ))
                if args.once:
                    break
                cooldown = max(60, int(os.getenv("RECON_RUN_COOLDOWN_SECONDS", "3600")))
                logging.getLogger(__name__).info(
                    "recon run window ended (targets=%d cap=%d); cooling down %ds",
                    budget.targets, budget.max_targets, cooldown,
                )
                await asyncio.sleep(cooldown)
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
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--schema-init", action="store_true")
    args = parser.parse_args()
    if args.schema_init:
        if args.dry_run or args.healthcheck:
            parser.error("--schema-init cannot be combined with --dry-run or --healthcheck")
        asyncio.run(_run_schema_init())
        return
    if args.healthcheck:
        try:
            healthy = asyncio.run(_check_worker_health())
        except Exception:
            logging.getLogger(__name__).exception("SpiderFoot healthcheck failed")
            healthy = False
        raise SystemExit(0 if healthy else 1)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
