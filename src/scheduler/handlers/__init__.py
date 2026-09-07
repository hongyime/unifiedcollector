"""Periodic handler registry for the scheduler.

Each entry is an instance of `PeriodicHandler` (see `base.py`). The scheduler's
``_tick`` loop iterates this list, calling ``should_run(ctx)`` then ``run(ctx)``
for each handler. Fault isolation: a failure in one handler must not stop
subsequent handlers on the same tick — the dispatcher wraps each call.

Registry is populated by handler modules on import (via ``HANDLERS.append(...)``)
so adding a new periodic job is a pure additive change: one new file, one
import, no edits to a monolithic ``_tick``.

All periodic handlers listed in docs/plans/scheduler-refactor.md are now
extracted. Step 15 will collapse the residual ``Scheduler`` shims.
"""
from __future__ import annotations

from .base import PeriodicHandler, SchedulerContext
from .bridge_unpaired_alert import BridgeUnpairedAlertHandler
from .cookie_check import CookieCheckHandler
from .gc_collection_runs import GcCollectionRunsHandler
from .graph_edges import BuildGraphEdgesHandler
from .heartbeat import HeartbeatHandler
from .phone_intel import PhoneIntelHandler
from .postgres_idle_txn_alert import PostgresIdleTxnAlertHandler
from .realtime_feed_alert import RealtimeFeedAlertHandler
from .recon_seed import ReconSeedHandler
from .reconcile_identities import ReconcileIdentitiesHandler
from .status_delta import StatusDeltaHandler
from .watchdog_stale_alert import WatchdogStaleAlertHandler

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
    WatchdogStaleAlertHandler(),
    PostgresIdleTxnAlertHandler(),
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
    "WatchdogStaleAlertHandler",
    "PostgresIdleTxnAlertHandler",
]
