"""Periodic handler registry for the scheduler.

Each entry is an instance of `PeriodicHandler` (see `base.py`). The scheduler's
``_tick`` loop iterates this list, calling ``should_run(ctx)`` then ``run(ctx)``
for each handler. Fault isolation: a failure in one handler must not stop
subsequent handlers on the same tick — the dispatcher wraps each call.

Registry is populated by handler modules on import (via ``HANDLERS.append(...)``)
so adding a new periodic job is a pure additive change: one new file, one
import, no edits to a monolithic ``_tick``.

TODO (follow-up sprint, docs/plans/scheduler-refactor.md steps 6-15):
  - WatchdogStaleAlertHandler        (`_maybe_alert_watchdog_stale`)
  - Then step 15: reduce Scheduler to __init__/start/stop/_tick + schedules CRUD.
"""
from __future__ import annotations

from .base import PeriodicHandler, SchedulerContext
from .bridge_unpaired_alert import BridgeUnpairedAlertHandler
from .cookie_check import CookieCheckHandler
from .gc_collection_runs import GcCollectionRunsHandler
from .graph_edges import BuildGraphEdgesHandler
from .heartbeat import HeartbeatHandler
from .phone_intel import PhoneIntelHandler
from .realtime_feed_alert import RealtimeFeedAlertHandler
from .recon_seed import ReconSeedHandler
from .reconcile_identities import ReconcileIdentitiesHandler
from .status_delta import StatusDeltaHandler

HANDLERS: list[PeriodicHandler] = [
    HeartbeatHandler(),
    StatusDeltaHandler(),
    ReconcileIdentitiesHandler(),
    CookieCheckHandler(),
    GcCollectionRunsHandler(),
    ReconSeedHandler(),
    PhoneIntelHandler(),
    BuildGraphEdgesHandler(),
    BridgeUnpairedAlertHandler(),
    RealtimeFeedAlertHandler(),
]

__all__ = [
    "HANDLERS",
    "PeriodicHandler",
    "SchedulerContext",
    "HeartbeatHandler",
    "StatusDeltaHandler",
    "ReconcileIdentitiesHandler",
    "CookieCheckHandler",
    "GcCollectionRunsHandler",
    "ReconSeedHandler",
    "PhoneIntelHandler",
    "BuildGraphEdgesHandler",
    "BridgeUnpairedAlertHandler",
    "RealtimeFeedAlertHandler",
]
