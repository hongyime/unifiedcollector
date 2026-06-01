# Plan: Detect & auto-heal the "zero-progress wedge" in the collector watchdog

**Date:** 2026-06-01
**Repo:** C:\unifiedcollector
**Target file:** `src/worker/__init__.py` (WorkerService)
**Tests:** `tests/test_watchdog_autoheal.py`
**Status:** PLAN ONLY — no code changed.

---

## Goal

Close the auto-heal gap discovered on 2026-05-31: under sustained host load (WSL2
thrashing) the collectors wedged into a state where the watchdog logged
`"<source> finished, relaunching"` every 30s but **did no real work** — `media_items`
writes stalled for ~66 minutes, Telegram threw `database is locked` / `SESSION DRIFT
0/4 connected`, and YouTube spun fetching with zero writes. A manual `docker restart`
recovered every source. The watchdog should have caught and recovered this **without
human intervention** — that's the whole point of the auto-heal deliverable.

## Current context / assumptions

The watchdog (`src/worker/__init__.py`) today recovers exactly two failure modes:

1. **Crashed task** (raised an exception) — `_run_source` increments `_crash_counts`,
   backs off, relaunches; watchdog's done-branch (lines 264-274) also relaunches a
   finished task while `crashes < max_restarts`.
2. **Hung task** (frozen inside `collector.run`, no exception, not `.done()`) — watchdog
   hang-detection (lines 276-306) cancels + relaunches when the per-source heartbeat
   (`self._heartbeat[source]`) is staler than `self.hang_timeout` (default 1800s).

**The gap:** a third mode — the *zero-progress clean finish*. The cycle EXITS CLEANLY
(`task.done()` true, no exception, `crashes` low) so it falls into the benign
done-branch and relaunches, but each relaunched cycle ALSO does no real work because the
wedge is in the DB/session layer, not the task. Hang-detection never fires because the
task keeps finishing fast (heartbeat stays fresh — it's updated at lines 198, 209, 211
every cycle), never crossing the 1800s `hang_timeout`. Result: infinite
finish→relaunch with no output, invisible to both existing recovery paths.

**Key code references (current):**
- Run loop: `_run_source` lines 189-237. Heartbeats set at 198 / 209 / 211.
  Post-cycle sleep `asyncio.wait_for(self._stop.wait(), timeout=300)` line 215.
- Watchdog done-branch: lines 264-274 (relaunch on finish/death).
- Watchdog hang-branch: lines 276-306 (cancel on stale heartbeat).
- Constructor knobs: `watchdog_interval=30` (line 26), `max_restarts=5` (line 27),
  `hang_timeout` from `COLLECTOR_HANG_TIMEOUT_SECONDS` default 1800 (line 40).

**Critical design constraint — the false-positive trap:** A cycle that finishes having
written nothing is NOT always a wedge. It's the *normal, correct* state when targets are
dedup-exhausted (e.g. YouTube re-scanning the 4 demo channels already fully collected —
4990 items, nothing new to write). Any "zero progress = restart" heuristic that can't
tell legitimate idle from a real wedge will thrash healthy collectors. **We need a
real-work signal, not just "cycle completed."**

## Proposed approach

Add a **per-source progress counter** distinct from the liveness heartbeat. The
heartbeat answers "is the task alive?"; the new counter answers "did the task accomplish
anything?". The watchdog escalates only when a source completes N consecutive cycles
that (a) had real targets to work AND (b) produced zero progress.

### What counts as "progress"
`collector.run(targets)` should return (or expose) a count of items actually
persisted/ingested this cycle. The cleanest signal is **rows written**, because that's
exactly what stalled in the incident. Options, in order of preference:

- **A (preferred):** `collector.run()` returns an int (items persisted this cycle), or
  the collector exposes `collector.last_run_persisted` / increments a counter the worker
  reads after `await collector.run(targets)` at line 210. Requires checking the
  `Collector` base class / each collector's `run` signature.
- **B (fallback, no collector changes):** the worker samples `SELECT COUNT(*) FROM
  media_items WHERE source=$1` (or the source's real table) before and after the cycle
  and diffs. Downside: not all sources write `media_items` (strava, telegram, beeper
  write their own tables) — would need a per-source table map, and the count query adds
  DB load. Less clean; use only if A is infeasible.

### Escalation logic (watchdog)
Track `self._zero_progress_streak[source]`. After each cycle in `_run_source`:
- If the cycle **had targets** (`len(targets) > 0`) and **persisted 0** → increment streak.
- If it persisted > 0, OR there were **no targets** (legit idle) → reset streak to 0.

In the watchdog done-branch, before the normal relaunch: if
`self._zero_progress_streak[source] >= ZERO_PROGRESS_LIMIT` (default 5), treat it like a
hang — escalate. Escalation ladder:
1. First escalation: cancel the task and relaunch (in-process reset — clears a wedged
   asyncpg pool connection / telethon session handle held by that task).
2. If in-process relaunch doesn't clear it (streak keeps climbing past a second
   threshold, e.g. 2x limit): **exit the process** (`os._exit` / signal the supervisor)
   so Docker's `restart: unless-stopped` recreates the container — this is what actually
   fixed it manually. A full process restart releases OS-level wedges (sqlite locks,
   sockets) that an in-process task cancel cannot.

### Why a process-exit tier is justified
The incident proved the in-process layer wasn't the wedge — `docker restart` was the fix,
not anything reachable from inside the event loop. Telethon's `database is locked` is a
SQLite file-lock at the OS level; a task cancel won't release it. So the recovery MUST
have a "nuke the process, let Docker restart" tier. Guard it hard (high threshold,
require the in-process tier to have been tried first) so it can never become a crash loop.

## Step-by-step plan

1. **Inspect the collector contract.** Read `src/collectors/__init__.py` (base
   `Collector`, `get_collector`) and 2-3 concrete collectors (youtube, lemon8, tiktok)
   to see what `run(targets)` returns today and whether a persisted-count is already
   available. Decide A vs B. (read-only)

2. **Add progress signal (approach A).** If `run` returns nothing, change it to return
   `int` (items persisted) OR add `self._persisted_this_run` on the base collector that
   `persist`/`upsert` increments and `run` resets. Smallest viable change that gives the
   worker a real number after line 210.

3. **Worker state.** In `WorkerService.__init__` add:
   - `self._zero_progress_streak: dict[str, int] = {}`
   - knobs: `self.zero_progress_limit = int(os.getenv("COLLECTOR_ZERO_PROGRESS_LIMIT", "5"))`
     and `self.zero_progress_hard_limit = int(os.getenv("COLLECTOR_ZERO_PROGRESS_HARD_LIMIT", "12"))`.

4. **Run-loop accounting.** In `_run_source` after `await collector.run(targets)` (line
   210): read persisted count; if `len(targets) > 0 and persisted == 0` increment the
   streak, else reset it. Leave the no-targets branch (line 200-206) resetting the streak
   (legit idle, never escalate).

5. **Watchdog escalation.** In the done-branch (lines 264-274), before relaunching:
   - if streak >= `zero_progress_limit` and < `zero_progress_hard_limit`: log a WARNING,
     reset crash backoff appropriately, relaunch fresh (already happens) but ALSO drop
     the source's pooled connection / force-recreate the collector so the relaunch gets a
     clean handle (set `collector = get_collector(source)` fresh — currently the collector
     is created once at line 193 and reused across cycles; a wedged handle survives
     relaunch unless we rebuild it).
   - if streak >= `zero_progress_hard_limit`: log ERROR, `_mark_source_dead` (or a new
     "wedged" status), then trigger process exit so Docker restarts the container. Reset
     streak first so a fresh container starts clean.

6. **Make the collector rebuild on relaunch.** Currently `get_collector(source)` is
   called once (line 193) outside the while-loop, so the SAME collector object (and its
   pool/session handles) is reused. Move/refresh collector construction so an escalated
   relaunch gets a brand-new collector — otherwise in-process relaunch can't clear a
   handle-level wedge. (This is arguably the single most important fix.)

7. **Tests.** Extend `tests/test_watchdog_autoheal.py`:
   - zero-progress streak increments only when targets present + persisted 0.
   - streak resets on any progress and on no-targets idle.
   - at `zero_progress_limit` → escalation (collector rebuilt / relaunched).
   - at `zero_progress_hard_limit` → process-exit path invoked (mock the exit hook;
     assert it's called once, not in a loop).
   - dedup-exhausted simulation (targets present, persisted 0, but this is "expected") —
     confirm we DON'T thrash if we add an "expected-empty" exemption (open question below).

8. **Deploy + observe (Pattern B bake).** Rebuild collector image, `compose up` all 3
   collectors, watch logs for the new WARNING/ERROR escalation lines, confirm no
   false-positive restarts on YouTube's dedup-exhausted demo channels over a multi-hour
   window.

## Files likely to change

- `src/worker/__init__.py` — primary: state, run-loop accounting, watchdog escalation,
  collector rebuild on relaunch.
- `src/collectors/__init__.py` (and/or individual collectors) — expose persisted-count
  from `run()`. Scope depends on step 1 findings.
- `tests/test_watchdog_autoheal.py` — new test cases.
- `docker/docker-compose.yml` — optionally document the two new env knobs on collectors.
- Skill `unifiedcollector-operations` ref `references/autoheal-and-file-config.md` —
  update the "KNOWN GAP" section to "FIXED" with the new knobs once shipped.

## Tests / validation

- `ast.parse` + `ruff check --select E9,F821 src/worker/__init__.py`.
- `pytest tests/test_watchdog_autoheal.py` (runs on host; containers lack pytest).
- Empirical: after deploy, force a wedge if reproducible (or wait for organic load) and
  confirm auto-recovery with no manual restart; confirm YouTube dedup-idle does NOT
  trigger escalation.

## Risks, tradeoffs, open questions

- **False positives are the #1 risk.** Dedup-exhausted sources legitimately persist 0
  with targets present. Mitigations to decide: (a) treat "all targets already collected"
  as a distinct collector return so it resets the streak; (b) only count a cycle as
  zero-progress if it ALSO made API/network calls but wrote nothing (needs collector
  cooperation); (c) make YouTube mark fully-collected targets `completed` so they stop
  appearing in `_load_targets` (lines 242-245 already filter to `pending`/`error`) —
  this is arguably the *correct* upstream fix and would make the demo-channel idle a
  no-targets reset, sidestepping the whole problem. **Recommend pursuing (c) in parallel.**
- **Process-exit tier is a loaded gun.** A bug here = container crash loop. Guard with a
  high hard-limit, require the soft tier first, and reset the streak before exit so the
  fresh container doesn't immediately re-escalate. Consider a minimum-uptime guard
  (don't process-exit within first N minutes of boot).
- **Collector rebuild on relaunch (step 6)** may surface latent assumptions that the
  collector is a singleton (caches, warmed clients). Need to verify get_collector is
  cheap and side-effect-free to call repeatedly.
- **Approach B's per-source table map** is brittle (strava/telegram/beeper write
  non-media_items tables). Strongly prefer A.
- **Open: is the wedge even reproducible on demand?** It was load-induced. If we can't
  reproduce, validation leans on the unit tests + long observation. A synthetic wedge
  (monkeypatch a collector to sleep-and-return-0 with targets) covers the logic but not
  the real OS-lock scenario.
- **Open: heartbeat vs progress overlap.** Confirm the new progress counter doesn't
  duplicate/conflict with the existing `_heartbeat` semantics — they answer different
  questions (alive vs productive) and both should remain.
