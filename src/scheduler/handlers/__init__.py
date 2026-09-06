"""Periodic handler registry for the scheduler.

Each entry is an instance of `PeriodicHandler` (see `base.py`). The scheduler's
``_tick`` loop iterates this list, calling ``should_run(ctx)`` then ``run(ctx)``
for each handler. Fault isolation: a failure in one handler must not stop
subsequent handlers on the same tick — the dispatcher wraps each call.

Registry is populated by handler modules on import (via ``HANDLERS.append(...)``)
so adding a new periodic job is a pure additive change: one new file, one
import, no edits to a monolithic ``_tick``.

TODO (follow-up sprint, docs/plans/scheduler-refactor.md steps 6-15):
  - ReconcileIdentitiesHandler       (`_maybe_reconcile_identities`)
  - CookieCheckHandler               (`_maybe_check_cookies`)
  - GcCollectionRunsHandler          (`_gc_collection_runs`)
  - BuildGraphEdgesHandler           (`_build_graph_edges`)
  - ReconSeedHandler                 (`_maybe_seed_recon_targets`)
  - PhoneIntelHandler                (`_maybe_run_phone_intel`)
  - BridgeUnpairedAlertHandler       (`_maybe_alert_bridge_unpaired`)
  - RealtimeFeedAlertHandler         (`_maybe_alert_realtime_feed_failed`)
  - WatchdogStaleAlertHandler        (`_maybe_alert_watchdog_stale`)
  - Then step 15: reduce Scheduler to __init__/start/stop/_tick + schedules CRUD.
"""
from __future__ import annotations

from .base import PeriodicHandler, SchedulerContext

HANDLERS: list[PeriodicHandler] = []

__all__ = ["HANDLERS", "PeriodicHandler", "SchedulerContext"]
