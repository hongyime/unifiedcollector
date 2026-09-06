"""CLI subcommand registration for unifiedcollector.

This package splits the 22 argparse subcommands that historically lived in
``src/main.py`` into small, cohesive modules under ``src/cli/commands/``.
Each module exposes:

* ``register(subparsers)`` — attaches its subparsers to the top-level parser.
* ``HANDLERS`` — a ``{command_name: callable(args)}`` dispatch dict. Handlers
  may be sync or async; the top-level dispatcher runs coroutines with
  ``asyncio.run``.

``register_all(subparsers)`` walks every command module, registers its
subparsers, and returns the merged dispatch dict for ``src/main.py`` to use.
"""
from __future__ import annotations

from src.cli.commands import (
    backfill_discovered_links,
    core_ops,
    coverage,
    media_repair,
    optional_rollout,
    realtime,
    rebuild,
    recon,
    restore,
    schedule_target,
)

_MODULES = [
    core_ops,
    coverage,
    recon,
    optional_rollout,
    realtime,
    rebuild,
    media_repair,
    backfill_discovered_links,
    restore,
    schedule_target,
]


def register_all(subparsers) -> dict:
    """Register every command group and return the merged dispatch dict."""
    handlers: dict = {}
    for mod in _MODULES:
        mod.register(subparsers)
        handlers.update(mod.HANDLERS)
    return handlers


__all__ = ["register_all"]
