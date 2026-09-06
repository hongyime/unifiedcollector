# Plan: Scheduler refactor — extract per-tick handler classes

Addresses **LOGIC-005**.

## 1. Context — current state

- File: `src/scheduler/__init__.py`, 99,099 B (~2,000 LOC).
- Single `Scheduler` class (line 182) owns everything.
- Class methods identified via grep:

| Method | Line | Role |
|---|---|---|
| `__init__` | 185 | pool, stop event, check_interval=60 |
| `start` | 190 | lifecycle entry; init DB; register conditional collectors; signal handlers |
| `_notify_startup_safe` / `_notify_shutdown_safe` | 277 / 284 | notifications |
| `_maybe_heartbeat` | 291 | hourly digest gate |
| `_maybe_status_delta` | 308 | 15-min delta gate |
| `_maybe_reconcile_identities` | 335 | identity_reconcile.py invocation |
| `_maybe_check_cookies` | 365 | cookie_health.py invocation |
| `_build_status` | 382 | assemble digest payload |
| `_build_status_delta` | 1155 | assemble delta payload |
| `_delta_per_source_counts` | 1217 | delta helper |
| `_delta_new_cooldowns` | 1280 | delta helper |
| `_delta_new_dead_sources` | 1324 | delta helper |
| `_delta_extension_hooks` | 1343 | delta helper |
| `_init_db` | 1369 | schema init |
| `_gc_collection_runs` | 1376 | GC old rows |
| `_register_beeper_if_enabled` | 1404 | conditional startup |
| `_register_strava_feed_if_enabled` | 1440 | conditional startup |
| `_tick` | 1463 | main dispatcher loop |
| `_build_graph_edges` | 1542 | scheduled graph rebuild |
| `_maybe_seed_recon_targets` | 1734 | ~6h recon seed |
| `_maybe_run_phone_intel` | 1774 | ~12h phone-OSINT sweep |
| `_maybe_alert_bridge_unpaired` | 1797 | bridge health alerter |
| `_maybe_alert_realtime_feed_failed` | 1849 | realtime feed alerter |
| `_maybe_alert_watchdog_stale` | 1894 | watchdog freshness alerter |
| `add_schedule` / `remove_schedule` / `list_schedules` | 1952+ | per-source schedule CRUD |

## 2. Motivation

- Every new periodic job adds a method to this class → violates SRP; class steadily grows.
- `_tick` is the dispatcher — a single bug there halts every job. Blast radius is the whole scheduler.
- Unit-testing a single handler (e.g. phone-OSINT sweep) requires instantiating the full `Scheduler`, mocking pool + notifications + all other periodic gates.
- Adding a new job means editing a 2k-LOC file, threading through `_tick`, and being sure not to trip another `_maybe_*` timer.
- Status-building helpers (`_build_status`, `_delta_*`) are reporting utilities, not schedulers — they don't belong here at all.

## 3. Target end state

```
src/scheduler/
  __init__.py                    # thin Scheduler orchestrator (<200 LOC)
                                 #   - loads handler registry
                                 #   - runs _tick loop
                                 #   - calls handler.should_run() then handler.run(ctx)
                                 #   - owns add/remove/list_schedules (per-source)
  status_builder.py              # _build_status, _build_status_delta, _delta_* (reporting utilities)
  startup.py                     # _init_db, _register_beeper_if_enabled, _register_strava_feed_if_enabled, _notify_startup/_shutdown
  handlers/
    __init__.py                  # HANDLERS: list[PeriodicHandler] registry
    base.py                      # PeriodicHandler ABC/Protocol
    heartbeat.py                 # HeartbeatHandler (hourly digest)
    status_delta.py              # StatusDeltaHandler (15-min delta)
    reconcile_identities.py
    cookie_check.py
    gc_collection_runs.py
    graph_edges.py
    recon_seed.py                # ~6h
    phone_intel.py               # ~12h
    bridge_unpaired_alert.py
    realtime_feed_alert.py
    watchdog_stale_alert.py
```

`PeriodicHandler` protocol:

```python
class PeriodicHandler(Protocol):
    name: str                     # for logging / metrics
    async def should_run(self, ctx: SchedulerContext) -> bool: ...
    async def run(self, ctx: SchedulerContext) -> None: ...
```

`SchedulerContext` carries `pool`, `now`, `notifier`, `stop_event`, and small typed getters — passed by the orchestrator; handlers do not reach into `Scheduler.self`.

## 4. Sequenced steps (commit-per-step)

1. `feat(scheduler): create handlers package + PeriodicHandler ABC + SchedulerContext dataclass` — new files only. No behavior change.
2. `refactor(scheduler): move _build_status, _build_status_delta, and _delta_* helpers to status_builder.py; re-import from __init__.py` — pure move; verify hourly/15-min digest content unchanged.
3. `refactor(scheduler): move _init_db, _register_beeper/_strava, _notify_startup/_shutdown to startup.py; Scheduler.start now delegates` — startup path is now importable and testable in isolation.
4. `refactor(scheduler): extract HeartbeatHandler` — first real handler cut. Registry has one entry. `_tick` uses registry for heartbeat only; every other `_maybe_*` still lives on `Scheduler`.
5. `refactor(scheduler): extract StatusDeltaHandler`.
6. `refactor(scheduler): extract ReconcileIdentitiesHandler`.
7. `refactor(scheduler): extract CookieCheckHandler`.
8. `refactor(scheduler): extract GcCollectionRunsHandler`.
9. `refactor(scheduler): extract ReconSeedHandler`.
10. `refactor(scheduler): extract PhoneIntelHandler`.
11. `refactor(scheduler): extract BuildGraphEdgesHandler`.
12. `refactor(scheduler): extract BridgeUnpairedAlertHandler`.
13. `refactor(scheduler): extract RealtimeFeedAlertHandler`.
14. `refactor(scheduler): extract WatchdogStaleAlertHandler`.
15. `refactor(scheduler): reduce Scheduler class to __init__/start/stop/_tick dispatcher + add/remove/list_schedules` — remove now-dead `_maybe_*` shims.

## 5. Rollback per step

Each step is one atomic commit that:
- Adds the new handler file with the extracted logic.
- Removes the corresponding `_maybe_*` method from `Scheduler`.
- Adds the handler to the `HANDLERS` registry.
- Updates `_tick` to route through the registry for that concern.

Rollback: `git revert <commit>` — restores the `_maybe_*` method and removes the handler file + registry entry atomically. No half-state is ever committed.

Special guard for step 15 (final consolidation): only run this step after 48 hours in staging with the new dispatch path — this is the point where legacy `_maybe_*` shims are deleted for good. Keep the previous commit tagged (`scheduler-pre-step-15`) as a durable rollback point.

## 6. Test strategy

- Existing: `tests/test_scheduler_graph_edges.py` must pass after every commit.
- New per-handler unit tests: `tests/scheduler/handlers/test_<handler>.py` — instantiate handler with a mocked `SchedulerContext`; assert `should_run` boundary behavior and `run` side effects (via mocked pool/notifier).
- Dispatch integration test: `tests/scheduler/test_dispatch.py` — build a scheduler with a fake registry of two handlers where handler A raises; assert handler B still runs on the same tick. This locks in fault-isolation, one of the motivating benefits.
- Live smoke: after each commit, boot `docker compose up -d scheduler`. Tail logs for 15 min; verify each extracted handler fires on its expected interval (heartbeat = 1h too slow for CI, but reconcile/cookie/gc all fire more frequently).
- Migration verification: at step 4 and step 15, capture a 10-min log window from `docker logs unifiedcollector_scheduler`. The multiset of `INFO scheduler: <handler>` messages must match between the two windows for handlers that are timer-driven and independent of external state.

## 7. Effort estimate + confidence

- **6–8 dev days.** Confidence: **HIGH**.
- The class already contains natural handler-shaped methods. Each extraction is ~50–150 LOC of moved code plus a small ABC. No new external dependencies. No user-visible behavior change (identical intervals, identical DB queries).
- Risk: the `_tick` method (line 1463) may contain implicit ordering assumptions (e.g., heartbeat runs before status_delta so both see the same DB snapshot). Extraction must preserve tick ordering. Mitigation: read `_tick` fully before step 4; port its ordering directly into the registry iteration order.
