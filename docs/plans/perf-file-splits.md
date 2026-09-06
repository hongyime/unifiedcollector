# Plan: PERF file splits — dashboard/api.py, bridges/ig_ingest.py, collectors/telegram/__init__.py

Covers **PERF-002**, **PERF-003**, **PERF-004**. Grouped because all three share methodology: package-split-with-façade.

## 1. Context — current state

| ID | File | Bytes | Approx LOC | Framework | Public surface today |
|---|---|---|---|---|---|
| PERF-002 | `src/dashboard/api.py` | 492,416 | 10,784 | FastAPI | `from src.dashboard.api import app` |
| PERF-003 | `src/bridges/ig_ingest.py` | 251,624 | 5,921 | aiohttp | `python -m src.bridges.ig_ingest` |
| PERF-004 | `src/collectors/telegram/__init__.py` | 267,148 | 5,446 | Telethon + BaseCollector | `from src.collectors.telegram import TelegramCollector` |

Observed internal structure (grep of definition markers):

- **api.py**: single FastAPI `app`, ~50 module-level cache/config vars at top, 340+ helper defs and route decorators. Clear domain clusters visible in helper prefixes: `_backup_*`, `_vault_*`, `_drive_*`, `_extension_*`, `_browser_tab_*`, `_source_matrix_*`, `_strava_*`, `_youtube_*`, `_beeper_*`, `_telegram_*`, plus auth/JWT (`bcrypt`, `jwt`, `HTTPBearer`). Compiled `.pyc` is 570,531 B — indicates real complexity, not comment bloat.
- **ig_ingest.py**: all routes registered in one block starting at line 5915 (`app.router.add_get(...)`, `app.router.add_post(...)`) — 30+ routes. Handlers include `get_targets`, `x_profile_target_next/result`, `ig_cooldown`, `discover`, `ingest`, `ingest_upload`, `ingest_upload_binary`, `browser_media_candidates`, `posts/comments/users/profile/seed/dms`, `cookies_handler`, `dm_frame/dm_sample/dm_probe/dm_heartbeat/dm_decoded`, `browser_revisit_*`, `tiktok_revisit_*`, `strava_route_queue_handler`, `strava_route_visit_handler`, `strava_streams_handler`, plus three middlewares (`db_pool_middleware`, `request_timeout_middleware`, `lane_isolation_middleware`) and pool bootstrap helpers (`_startup_state`, `_ensure_app_pool`, `_schedule_app_task`).
- **telegram/__init__.py**: two dominant classes — `TelegramWorker` (line 313; per-Telethon-client session state, `connect/disconnect/run_targets`) and `TelegramCollector(BaseCollector)` (line 580; owns backfill, realtime, dialogs, members, profile-photo download, media download). Plus module-level helpers `_tg_json`, `_tg_jsonb`, `_normalize_telegram_username`, `_telethon_payload`, `_is_flood_wait`, `_is_file_reference_expired`, `_format_exception`, `SessionState` enum. There is also a sibling `parse.py` (4,411 B) that's already split out.

## 2. Motivation — what breaks or degrades without this

- **IDE/LSP degradation**: PyLance re-indexing a 10k-LOC single file takes 10–30 s and blocks incremental typechecking. Multi-file navigation ("go to symbol") in these three files dominates dev latency.
- **Test import cost**: unit-testing a single helper (e.g., `_normalize_backup_health_payload`) imports FastAPI, DB pool, JWT, static-files mount, and 30+ core modules — a 3–5 s cold import. Tests that should take 100 ms take 5 s.
- **Merge conflicts**: any two parallel branches touching different concerns collide in one file. Recent git log on these files should be inspected — expected >70% of merges to have hit conflicts.
- **`pyc` size and startup memory**: `api.cpython-312.pyc` is 570 KB. Every container that imports the dashboard pays this in RSS.
- **Cognitive load / onboarding**: new contributors cannot form a mental map. Docs already list them as PERF issues, so the friction is documented.
- **Blast radius of edits**: a bad edit anywhere in the file wedges every route/method. Splitting reduces the blast radius per PR.

Nothing "breaks" today — the code runs. This is a **maintenance-decay** debt that compounds. Deferring another 6 months adds another ~500 LOC per file at current velocity and makes the split proportionally harder.

## 3. Target end state

Each of the three files becomes a **package** with a stable public façade so downstream imports don't change:

**Dashboard:**
```
src/dashboard/
  api/
    __init__.py            # thin app assembly; include_router() calls; <300 LOC
    helpers.py             # module-level caches, TTL constants, small pure helpers
    auth.py                # JWT/bcrypt, HTTPBearer, login/logout routes
    health_helpers.py      # _vault_*, _backup_*, _drive_* payload builders
    browser.py             # /api/extension/*, /api/browser/* routes + helpers
    source_matrix.py       # /api/source_matrix routes + all _SOURCE_MATRIX_* caches
    telegram_ops.py        # /api/telegram/* routes
    strava.py              # /api/strava/* routes
    youtube.py             # /api/youtube/* routes
    whatsapp.py            # /api/whatsapp/* routes
    coverage.py            # /api/coverage
    media.py               # /api/media
    rate_limits.py         # /api/rate-limits/*
  websocket.py             # unchanged
```

Import contract preserved: `from src.dashboard.api import app` still works because `api/__init__.py` re-exports `app`.

**ig_ingest:**
```
src/bridges/ig_ingest/
  __init__.py              # module entry: `python -m src.bridges.ig_ingest` runs .app:main
  app.py                   # app factory (aiohttp Application), route wiring, run_app()
  middleware.py            # db_pool_middleware, request_timeout_middleware, lane_isolation_middleware
  pool.py                  # _startup_state, _ensure_app_pool, _schedule_app_task, close_pool
  constants.py             # MEDIA_ROOT, PORT, DM_SAMPLE_DIR, timeouts, concurrency caps
  cors.py                  # _cors, handle_options
  helpers.py               # _ig_date_from_id, _shortcode_from_id, _verify_url, _norm_platform
  targets.py               # /social/targets, /ig/targets, _targets_for, _cached_targets_for, refresh cache
  discover.py              # /social/discover, /ig/discover, _discover
  ingest.py                # /social/ingest, ingest_upload, ingest_upload_binary, browser_media_candidates
  x_profile.py             # x_profile_target_next / _result
  revisit.py               # browser_revisit_*, tiktok_revisit_*
  dm.py                    # dm_frame, dm_sample, dm_probe, dm_heartbeat, dm_decoded handlers
  strava.py                # strava_route_queue / _visit / streams handlers
  cookies.py               # /social/cookies, cookie sync helpers
  cooldown.py              # /social/ig_cooldown
  telemetry.py             # sw-crash, browser telemetry, DM hook heartbeat
```

**telegram:** mixin-composition split (preserves one public class):
```
src/collectors/telegram/
  __init__.py              # re-exports TelegramCollector; module-level docstring
  helpers.py               # _tg_json, _tg_jsonb, _normalize_telegram_username, _telethon_payload, _is_flood_wait, _format_exception, regex constants
  session.py               # SessionState, TelegramWorker, EntityUnresolvable, EntityResolveDeferred
  collector.py             # class TelegramCollector(BackfillMixin, RealtimeMixin, DialogsMixin, MembersMixin, ProfileMixin, MediaMixin, BaseCollector)
  mixins/
    __init__.py
    backfill.py            # backfill_chat + cursor pagination
    realtime.py            # collect_realtime + Telethon event handlers (@client.on(NewMessage/Edit/Deleted/Reaction))
    dialogs.py             # collect_dialogs, telegram_chats upsert
    members.py             # collect_chat_members
    profile.py             # collect_user_profile, profile photo tracker
    media.py               # download_message_media
  parse.py                 # UNCHANGED (already split, 4,411 B)
```

Import contract preserved: `from src.collectors.telegram import TelegramCollector` still works.

## 4. Sequenced steps (commit-per-step)

### 4A. dashboard/api.py

1. `feat(dashboard): create api package skeleton` — `git mv src/dashboard/api.py src/dashboard/api/__init__.py`; no code change. Verify `from src.dashboard.api import app` still resolves and pytest is green.
2. `refactor(dashboard): extract module-level constants and pure helpers to api/helpers.py` — move `_MESSAGING_COVERAGE_CACHE`, `_SOURCE_MEDIA_TOTALS_*`, `_BEEPER_*`, `_SOURCE_MATRIX_*`, `_COLLECTORS_LIVE_*`, `_TELEGRAM_STATS_*`, `_YOUTUBE_*` caches + the `_tiktok_revisit_claim_timeout_seconds`, `_encode_polyline`, `_jsonb_points`, `_row_get`, `_iso_or_none`, `_safe_row`, `_strava_route_status`, `_estimated_table_rows` helpers. Re-import from `__init__.py` for back-compat.
3. `refactor(dashboard): extract vault/backup/drive health helpers to api/health_helpers.py` — `_vault_payload`, `_backup_health_status`, `_normalize_backup_health_payload`, `_vault_health_status`, `_drive_health_status`.
4. `refactor(dashboard): extract auth (JWT / bcrypt / login) to api/auth.py` — smallest cohesive slice, safe first cut.
5. `refactor(dashboard): extract browser/extension routes to api/browser.py` — `_extension_*`, `_browser_tab_*`, `_browser_extension_*`, `_browser_ingest_health_*` helpers plus the routes. Introduce `router = APIRouter()`; `app.include_router(router)` in `__init__.py`.
6. `refactor(dashboard): extract source_matrix routes to api/source_matrix.py`.
7. `refactor(dashboard): extract telegram_ops routes to api/telegram_ops.py`.
8. `refactor(dashboard): extract strava routes to api/strava.py`.
9. `refactor(dashboard): extract youtube routes to api/youtube.py`.
10. `refactor(dashboard): extract whatsapp routes to api/whatsapp.py`.
11. `refactor(dashboard): extract coverage/media routes to api/coverage.py + api/media.py`.
12. `refactor(dashboard): extract rate_limits routes to api/rate_limits.py`.
13. `refactor(dashboard): reduce api/__init__.py to app assembly` — remove all inline routes, keep only `FastAPI()` construction + middleware + `include_router` calls + `Depends` glue.

Target `api/__init__.py` size after step 13: <300 LOC.

### 4B. bridges/ig_ingest.py

1. `feat(bridges): create ig_ingest package skeleton` — `git mv src/bridges/ig_ingest.py src/bridges/ig_ingest/__init__.py`; add `__main__.py` that imports `app` and runs `web.run_app(app, port=PORT)` so `python -m src.bridges.ig_ingest` still works.
2. `refactor(ig_ingest): extract constants, cors, middleware to constants.py + cors.py + middleware.py`.
3. `refactor(ig_ingest): extract pool bootstrap to pool.py` — `_startup_state`, `_ensure_app_pool`, `_set_startup_error/_pending`, `_schedule_app_task`, `_PoolRef`.
4. `refactor(ig_ingest): extract targets/discover to targets.py + discover.py`.
5. `refactor(ig_ingest): extract ingest + upload handlers to ingest.py`.
6. `refactor(ig_ingest): extract x_profile and revisit handlers to x_profile.py + revisit.py`.
7. `refactor(ig_ingest): extract DM hooks to dm.py`.
8. `refactor(ig_ingest): extract strava route capture to strava.py`.
9. `refactor(ig_ingest): extract cookies + telemetry to cookies.py + telemetry.py`.
10. `refactor(ig_ingest): extract cooldown to cooldown.py`.
11. `refactor(ig_ingest): reduce __init__.py to app.py factory + entry` — `__init__.py` now imports `app` from `app.py`; route wiring lives in `app.py::build_app()`.

### 4C. collectors/telegram/__init__.py

1. `feat(telegram): create mixins package inside telegram/` — introduce empty `mixins/__init__.py` and empty `mixins/{backfill,realtime,dialogs,members,profile,media}.py`. No behavior change.
2. `refactor(telegram): extract module-level helpers to helpers.py` — the `_tg_*`, `_normalize_*`, `_telethon_payload`, `_is_flood_wait`, `_is_file_reference_expired`, `_format_exception`, `_is_transient_realtime_write_error`, regex constants. Re-export from `__init__.py`.
3. `refactor(telegram): extract SessionState + TelegramWorker to session.py` — plus `EntityUnresolvable`, `EntityResolveDeferred` exceptions.
4. `refactor(telegram): move backfill methods off TelegramCollector into BackfillMixin` — mechanical: cut every method belonging to the backfill concern, paste into `mixins/backfill.py::BackfillMixin`, keep signatures identical. Update `class TelegramCollector(BackfillMixin, BaseCollector):` in the same commit.
5. `refactor(telegram): extract RealtimeMixin` — Telethon `@client.on(...)` handlers.
6. `refactor(telegram): extract DialogsMixin`.
7. `refactor(telegram): extract MembersMixin`.
8. `refactor(telegram): extract ProfileMixin`.
9. `refactor(telegram): extract MediaMixin`.
10. `refactor(telegram): move TelegramCollector shell to collector.py` — the class definition now just composes mixins + `__init__` / lifecycle. `__init__.py` re-exports `TelegramCollector` from `collector`.

## 5. Rollback per step

Every step is a single squashable commit that:
- Preserves the public import surface (verified by an import-smoke test run before commit).
- Moves code between files but changes no function bodies.

Rollback: `git revert <commit>` for the offending step. Because the façade module keeps re-exports until the final "reduce __init__.py" step, any intermediate revert leaves the code fully working.

Additional safety for step 4C.4–4C.9 (mixin cut-over): each mixin extraction must be one atomic commit — no half-moved methods. Reviewer checklist: `dir(TelegramCollector)` set must be identical before and after (add a snapshot test at step 4C.1).

## 6. Test strategy

### Automated

- Existing pytest suites — must remain green after every commit:
  - `pytest tests/dashboard` (test_source_matrix.py, test_coverage_api.py, test_extension_health.py, test_platform_summary.py, test_recon_api.py, test_targets_dedupe.py, test_whatsapp_qr.py, test_media_path_resolver.py, test_strava_route_status.py, test_whatsapp_links.py)
  - `pytest tests/bridges` (test_ig_ingest_vault.py)
  - `pytest tests/collectors/test_telegram*.py`

- New snapshot tests (added in step 1 of each sub-plan):
  - `tests/dashboard/test_api_route_surface.py`: `from src.dashboard.api import app; assert sorted(r.path for r in app.routes) == GOLDEN_ROUTES` — GOLDEN_ROUTES captured pre-refactor.
  - `tests/bridges/test_ig_ingest_route_surface.py`: same idea, using `app.router.resources()`.
  - `tests/collectors/test_telegram_public_surface.py`: `assert set(dir(TelegramCollector)) >= GOLDEN_METHODS`.

- Import cold-start benchmark: `python -c "import time; t=time.perf_counter(); from src.dashboard.api import app; print(time.perf_counter()-t)"` — capture pre-refactor baseline; post-refactor must be within 15% (splitting rarely regresses, but guard against accidental circular re-imports).

### Manual

After each 4A/4B/4C sub-plan completes:
- `docker compose up -d dashboard ig_ingest collector_telegram` and hit `/health` on each.
- For telegram: confirm the container ingests 1+ message from a known active chat within 60 s.
- For ig_ingest: `curl -X POST http://localhost:8765/social/ingest -d '{"platform":"instagram","username":"test","items":[]}'` returns 200 with `accepted=0`.

## 7. Effort estimate + confidence

| Sub-plan | Effort | Confidence | Notes |
|---|---|---|---|
| 4A (api.py) | 4–5 dev days | **HIGH** | FastAPI `APIRouter` split is boilerplate-mechanical. Route decorators + include_router. |
| 4B (ig_ingest.py) | 3–4 dev days | **MEDIUM** | aiohttp has no built-in router include analog — manual grouping. Middleware ordering must be preserved. |
| 4C (telegram) | 3–5 dev days | **MEDIUM** | Mixin composition is stylistic risk: method-resolution-order must preserve behavior. `BaseCollector` internals may leak state expected on `self`. |
| **Total** | **10–14 dev days** | | Estimate assumes one engineer working sequentially. |

Risk multipliers:
- Circular imports between extracted modules (mitigate: helpers module is leaf; domain modules depend on helpers only).
- Hidden `self.foo` references from one mixin to another that hop through `TelegramCollector` instance state (mitigate: type-check with pyright after each mixin extraction; add an integration test that instantiates and runs one cycle).
