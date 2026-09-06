"""Thin CLI dispatcher for unifiedcollector.

Every subcommand implementation lives under :mod:`src.cli.commands`. This
module owns only:

* Logging bootstrap and the ``_route_logs_to_stderr_for_json`` helper that
  keeps ``stdout`` machine-readable for ``--json`` invocations.
* :func:`init_db` — the single DDL/migration application entrypoint used by
  handler modules (via lazy imports) and by ``tools/optional_rollout_monitor.py``.
* :func:`main` — argparse construction + dispatch to the ``{command: handler}``
  dict returned by :func:`src.cli.register_all`.
"""
import argparse
import asyncio
import logging
import sys

from src.cli import register_all

# NOTE: P2-3 attempted a QueueHandler/QueueListener pipeline here but it
# deadlocked the collector (main thread stuck in futex_wait on the logging
# lock; 0% CPU freeze ~2 min after start). Reverted to the known-good simple
# config. The real original freeze mitigation is silencing chatty httpx/telethon
# INFO logging (below), which is preserved.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
# Keep verbose third-party loggers from flooding the event-loop log path.
for _noisy in ("httpx", "httpcore", "telethon", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("unifiedcollector")


def _route_logs_to_stderr_for_json() -> None:
    """Keep stdout machine-readable for JSON CLI commands."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) is sys.stdout:
            handler.setStream(sys.stderr)


async def init_db(pool):
    # P0-1/P0-2: single DDL authority. Applies base schemas/ then pending
    # migrations/ via the ledger-backed runner. The old code globbed schemas/
    # ONLY, silently omitting 19 live, code-referenced tables under migrations/.
    from src.db.migrate import apply_all
    from src.core.maintenance import run_collector_maintenance
    await apply_all(pool)
    await run_collector_maintenance(pool)


def main():
    parser = argparse.ArgumentParser(description="UnifiedCollector")
    sub = parser.add_subparsers(dest="command")
    handlers = register_all(sub)

    args = parser.parse_args()
    if getattr(args, "json", False):
        _route_logs_to_stderr_for_json()

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return

    result = handler(args)
    if asyncio.iscoroutine(result):
        asyncio.run(result)


if __name__ == "__main__":
    main()
