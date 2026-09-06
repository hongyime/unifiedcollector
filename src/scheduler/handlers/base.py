"""Protocol + shared context for periodic scheduler handlers.

`PeriodicHandler` is a `typing.Protocol` (structural typing) rather than an
abstract base class so handler modules do not need to import from here just to
participate — any object with the right shape counts. The registry in
``handlers/__init__.py`` is typed against this protocol for editor / mypy
support.

`SchedulerContext` is the per-tick bag of dependencies passed to every handler.
Handlers must not reach back into the ``Scheduler`` instance; everything they
need is on the context. This makes handlers unit-testable with a stub context —
no need to instantiate ``Scheduler`` or mock every unrelated periodic gate.

`get_env_int` / `get_env_float` are injected callables (defaults point to
``src.core.env``) so tests can substitute deterministic values without setting
process env vars.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class PeriodicHandler(Protocol):
    """Structural contract for a scheduler handler.

    Attributes:
      name: stable, human-readable identifier used in logs / metrics.

    Methods:
      should_run: cheap check (no DB, no I/O). Returns True when the handler's
        interval has elapsed. The scheduler calls this once per tick; if it
        returns False the handler is skipped for this tick.
      run: perform the actual work. Any exception raised here MUST be caught
        by the dispatcher — one failing handler must not stop the others.
    """

    name: str

    async def should_run(self, ctx: "SchedulerContext") -> bool: ...

    async def run(self, ctx: "SchedulerContext") -> None: ...


@dataclass
class SchedulerContext:
    """Per-tick dependency bag handed to every handler.

    Fields:
      pool: asyncpg pool (already connected).
      now: UTC datetime captured at the start of the tick, for consistent
        timestamps across handlers.
      notifier: reference to the notifications module (e.g. ``src.notifications.alerts``);
        typed as ``Any`` because import-cycle concerns keep it late-bound.
      stop_event: cooperative shutdown flag; handlers doing long work should
        check ``stop_event.is_set()`` between chunks.
      get_env_int / get_env_float: env-var readers with typed defaults + bounds.
        Injected so tests can pass deterministic stubs without touching os.environ.
    """

    pool: Any
    now: datetime
    notifier: Any
    stop_event: asyncio.Event
    get_env_int: Callable[..., int]
    get_env_float: Callable[..., float]


__all__ = ["PeriodicHandler", "SchedulerContext"]
