# pool-leak-fix — investigation & remediation

Postgres pool slots on the running unifiedcollector Postgres are being pinned by
long-lived `idle in transaction` (IIT) sessions. This plan pins the offending
call site to a single function, explains the exact leak mechanism against the
observed evidence, proposes the minimum-change fix, and adds a scheduler-side
alert so the next occurrence surfaces in minutes instead of hours.

- Status: proposed (2026-09-07)
- Owner: platform / collector
- Blast radius of fix: analyzer container restart + collector scheduler restart
- Code changes cross both repos: `unifiedanalyzer` (fix) + `unifiedcollector`
  (detection). Both are bind-mounted, so hot-reload works.

## Live-DB evidence recap

Gathered before this plan:

```sql
SELECT state, count(*)
FROM pg_stat_activity
WHERE state IN ('idle', 'idle in transaction')
  AND state_change < NOW() - INTERVAL '20 minutes';
-- 6 rows leaked (across states).
```

- 2 sessions `state='idle in transaction'`, running a query with
  `c.sha AS record_id, c.date AS occurred_at, LEFT(c.message, 20…)` against
  `github_commits`.
- `application_name` blank on all leaked rows.
- `client_addr = 172.22.0.1` (docker compose bridge gateway).

## Offending call site (evidence-anchored)

Leaked query — **exact literal match**, one hit in the workspace:

```
C:\unifiedanalyzer\src\pipeline\timeline_builder.py:37-44

    "query": """
        SELECT c.sha AS record_id, c.date AS occurred_at,
               LEFT(c.message, 200) AS title, c.author_login AS entity_ref
        FROM github_commits c
        WHERE c.date IS NOT NULL {where_clause}
        ORDER BY c.date DESC
    """,
```

Enclosing block — the site that acquires and holds the collector pool slot:

```
C:\unifiedanalyzer\src\pipeline\timeline_builder.py:766-853  (async def build_timeline)

766        try:
767            pool = analyzer if pq.get("db") == "analyzer" else collector
768            async with pool.acquire() as conn:                       # collector conn checked out
769                async with conn.transaction():                       # BEGIN issued
770                    cursor = conn.cursor(query, *params,              # server-side cursor
                                            timeout=SOURCE_QUERY_TIMEOUT_SECONDS)  # 1800s
                        ...
778                    async for row in cursor:
                            ...
823                        if len(batch) >= BATCH_SIZE:
824                            await _insert_batch(analyzer, batch)      # <-- acquires DIFFERENT pool
                                stats["inserted"] += len(batch)
                                batch.clear()
                    ...
828                    if batch:
829                        await _insert_batch(analyzer, batch)
                    ...
850        except Exception as e:                                       # does NOT catch CancelledError
851            logger.warning("Skipping %s: %s", source_name, e)
852            stats["skipped_tables"].append(source_name)
```

Pool config (context for why the interlock actually stalls):

```
C:\unifiedanalyzer\src\db\connection.py:41-47

    async def _create_pool_once(params: dict, max_size: int) -> asyncpg.Pool:
        min_size = max(1, max_size // 4)
        return await asyncpg.create_pool(
            **params,
            min_size=min_size,
            max_size=max_size,     # DB_MAX_POOL_SIZE, default 10
            ssl="disable",
            command_timeout=300,   # 5 min per-call default
        )
```

- No `server_settings={"application_name": …}` → matches the blank
  `application_name` on the leaked pg_stat_activity rows.
- No `idle_in_transaction_session_timeout` on the pool or the collector's
  `postgresql.conf` (grepped both repos: 0 hits) → Postgres never kills the
  orphaned session on its own.
- `client_addr 172.22.0.1` = docker compose bridge gateway; the analyzer
  container reaches the collector Postgres via the compose network, which
  presents as the gateway when hairpinning through a user-defined bridge.

The github variant is `PLATFORM_QUERIES[0]` (first entry, line 33+); it hits
`github_commits` (~7.3M rows per the note at line 717) which dominates cursor
lifetime, so it is the query most visible in `pg_stat_activity` snapshots. Every
other entry in `PLATFORM_QUERIES` runs through the same `try` block and is
susceptible to the same leak, just for shorter observed windows.

## Leak mechanism

Three failure modes stack on the same 88-line block. Any one is sufficient to
produce the observed symptom; the first is the primary driver.

### 1. Cross-pool interlock — primary

- Line 768 checks out a **collector-pool** connection.
- Line 769 opens a Postgres transaction (`BEGIN`) — required because line 770
  uses `conn.cursor(...)`, which asyncpg only permits inside an open txn.
- Inside the `async for row in cursor:` loop (lines 778–826) the code awaits
  `_insert_batch(analyzer, batch)` (lines 824, 829). `_insert_batch` at line
  855 does its own `async with pool.acquire() as conn:` — on the **analyzer**
  pool — then `await conn.executemany(...)` of 5000-row batches into the
  partitioned `timeline_events` table.
- While that second acquire blocks (analyzer `max_size=10`, and the same
  process is doing entity resolution, interaction graph, alerting, etc.
  concurrently — see `incremental_runner.py:831+`), the **first (collector)**
  connection is *idle in transaction* from Postgres's POV. It has already
  issued `BEGIN`, holds a cursor portal open, and is not sending anything to
  the server.
- Every second the analyzer executemany takes = one second of IIT time on the
  collector connection. Sustained analyzer-pool contention → 20+ min IIT.

Observed evidence lines up:

| Symptom | Mechanism prediction |
|---|---|
| 6 pooled connections in `idle` / `idle in transaction` | 1 IIT per running platform query × concurrent runs of `build_timeline` (incremental + full_resolution loops). |
| 2 hitting `github_commits` specifically | `PLATFORM_QUERIES[0]` — github is the longest-running cursor and the most likely to be caught in a snapshot. |
| Blank `application_name` | `_create_pool_once` sets no `server_settings`. |
| `client_addr 172.22.0.1` | docker bridge gateway ↔ analyzer container. |

### 2. `except Exception` misses cancellation — secondary

Line 850 catches `Exception`, not `BaseException`. In Python ≥ 3.8:

- `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit` inherit from
  `BaseException`, not `Exception`.
- Any `docker compose restart` / `docker compose down` / graceful SIGTERM /
  `run_incremental` task cancellation delivers `CancelledError` into the
  running coroutine.

When cancellation lands mid-iteration:

1. Python raises `CancelledError` at the current `await` (typically inside
   the `async for row in cursor:` or the awaiting `_insert_batch(analyzer,…)`).
2. `async with conn.transaction():` `__aexit__` runs and awaits
   `self.rollback()`. In current asyncpg this rollback is **not**
   `asyncio.shield`-ed — the cancellation re-fires on that await, so the
   `ROLLBACK` command may never be flushed to the server before the coroutine
   returns.
3. `async with pool.acquire() as conn:` `__aexit__` runs and awaits pool
   release / `_reset()`. Same cancellation risk.
4. The outer `except Exception:` at line 850 does not catch `CancelledError`,
   so no salvage step runs.
5. Postgres sees `BEGIN` + cursor bind, then silence. The session sits IIT
   until TCP keepalive tears it down (`tcp_keepalives_idle` default = 7200s).

This produces exactly the "IIT for 20+ min, then eventually cleaned up by
TCP" pattern the operator is seeing.

### 3. No Postgres-side safety net — silent-failure amplifier

- No `idle_in_transaction_session_timeout` at server, database, or role scope
  (grep in both repos: 0 hits).
- `command_timeout=300` on the pool applies only when a statement is actively
  running — an idle-in-transaction connection has no in-flight statement, so
  this timer does not fire.
- `SOURCE_QUERY_TIMEOUT_SECONDS = 1800` (line 10) applies to the cursor's
  `FETCH`, again only while a fetch is in flight.

Result: nothing on either side of the wire kills a leaked IIT session
proactively. It accretes until the pool is exhausted or the container
restarts.

## Tests are not the leak

Grep for `conn.transaction` in tests/:

- `C:\unifiedcollector\tests\collectors\test_beeper.py:256` — `MagicMock(return_value=_TxCtx())`; in-memory noop.
- `C:\unifiedcollector\tests\core\test_source_config.py:22` — `_transaction` is an `@asynccontextmanager` that yields nothing; no Postgres round-trip.
- `C:\unifiedanalyzer\tests\**` — zero hits.

No test opens a real Postgres transaction and forgets to close it.

## Minimum-change fix

Three edits, roughly 50 LOC total. Each is independently useful; applying all
three eliminates the class of failure.

### Fix A — Postgres-side safety net (2 lines, biggest ROI per line)

`unifiedanalyzer/src/db/connection.py:41-47` — extend the pool factory:

```python
async def _create_pool_once(params: dict, max_size: int) -> asyncpg.Pool:
    min_size = max(1, max_size // 4)
    return await asyncpg.create_pool(
        **params,
        min_size=min_size,
        max_size=max_size,
        ssl="disable",
        command_timeout=300,
        server_settings={
            "application_name": os.getenv("ANALYZER_APP_NAME", "unifiedanalyzer"),
            "idle_in_transaction_session_timeout": os.getenv(
                "PG_IIT_TIMEOUT_MS", "300000"),   # 5 min
        },
    )
```

Effect:

- Postgres kills any IIT session older than 5 min with
  `FATAL: terminating connection due to idle-in-transaction timeout`. asyncpg
  observes the connection loss and drops it from the pool.
- `application_name` shows up in `pg_stat_activity`, so future triage takes
  seconds instead of a grep-hunt.
- Optionally, per-call sites can override the app name via `SET LOCAL` to
  distinguish `timeline_builder` from `interaction_graph` etc.

This alone converts the current silent leak into a self-healing loud failure.
Ship it first.

### Fix B — Drop the cross-pool interlock (keyset paginate, ~30 LOC)

`unifiedanalyzer/src/pipeline/timeline_builder.py:766-829` — replace the
long-lived server-side cursor with keyset pagination so no transaction spans
the analyzer executemany.

Shape (pseudocode, not for commit):

```python
last_key = None                    # (occurred_at, record_id) high-water
while True:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            query_with_keyset,      # WHERE (time_col, id_col) < ($1,$2) LIMIT 5000
            *params_with_last_key,
            timeout=SOURCE_QUERY_TIMEOUT_SECONDS,
        )
    # collector connection is now RELEASED — no txn held

    if not rows:
        break
    batch = build_batch(rows)
    await _insert_batch(analyzer, batch)   # analyzer acquire happens with zero collector holdings
    last_key = (rows[-1]["occurred_at"], rows[-1]["record_id"])
```

Rationale over alternatives:

- **Buffer-all-then-insert**: not viable — 7.3M github rows × ~200 B ≈ 1.5 GB
  RSS.
- **Shorter transaction with `FETCH FORWARD 5000`**: still holds a txn across
  the analyzer executemany.
- **`autocommit`-style bare fetch (no txn)**: not supported for server-side
  cursors in asyncpg. Keyset does the same thing without asyncpg surface.

Per-source cost: each `PLATFORM_QUERIES` entry needs a `time_col` +
`record_id` composite key already declared in the dict (`time_col` is present
on every entry; the record-id column is the first `SELECT` column). Wiring a
generic `paginate(pq)` helper covers all 24 entries.

For `facetracker` (uses UNION ALL, `db=analyzer`, `time_filters`), keyset is
awkward — keep the current cursor path for that entry only, or paginate by
`occurred_at` alone. Facetracker is on the analyzer DB, so it does not
contribute to collector-pool leaks either way.

### Fix C — Cancellation-safe cleanup (5 LOC)

`unifiedanalyzer/src/pipeline/timeline_builder.py:766-853` — widen the
except clause and ensure a real close:

```python
try:
    ...
except (Exception, asyncio.CancelledError) as e:
    logger.warning("Skipping %s: %s", source_name, e)
    stats["skipped_tables"].append(source_name)
    if isinstance(e, asyncio.CancelledError):
        raise                    # do not swallow shutdown
```

`asyncio.CancelledError` is a `BaseException` in 3.8+, so it isn't currently
caught. Catching + re-raising gives the `async with` `__aexit__` chain a
best-effort rollback pass, while still propagating the cancel. Combined with
Fix A this is belt-and-braces — Postgres will time out the session even if
the rollback never lands.

## Proactive detection — new scheduler handler

Mirror `unifiedcollector/src/scheduler/handlers/bridge_unpaired_alert.py`.
Add `unifiedcollector/src/scheduler/handlers/postgres_idle_txn_alert.py`.

Registration in `unifiedcollector/src/scheduler/handlers/__init__.py` — add:

```python
from .postgres_idle_txn_alert import PostgresIdleTxnAlertHandler
...
HANDLERS: list[PeriodicHandler] = [
    ...
    PostgresIdleTxnAlertHandler(),
]
```

Handler shape (contract, not code):

- `name = "postgres_idle_txn_alert"`
- `should_run` gates on `PG_IIT_ALERT_INTERVAL_SECONDS` (default 900, min 300).
- `run` executes a single fetch against `ctx.pool` (the collector pool — this
  is the DB we want to protect):

  ```sql
  SELECT count(*)                              AS n,
         max(EXTRACT(EPOCH FROM (NOW() - xact_start))/60)::int
                                               AS oldest_min,
         array_agg(DISTINCT COALESCE(NULLIF(application_name,''),'<blank>'))
                                               AS apps,
         array_agg(DISTINCT client_addr::text) AS clients,
         array_agg(DISTINCT LEFT(query, 200))  AS samples
    FROM pg_stat_activity
   WHERE state = 'idle in transaction'
     AND xact_start < NOW() - ($1 || ' minutes')::interval;
  ```

- Threshold: `PG_IIT_ALERT_THRESHOLD` (default 2, min 1).
- Age: `PG_IIT_ALERT_AGE_MINUTES` (default 10, min 2).
- On breach → Telegram message via `src.notifications.telegram.send(…)`:

  ```
  ⚠️  Postgres idle-in-transaction leak
  {n} sessions idle-in-tx for > {age_min} min (oldest {oldest_min} min)
  apps:    {apps}
  client:  {clients}
  query:   {samples[0][:120]}...
  ```

- Same `last_fire` semantics as `bridge_unpaired_alert`: only advance the
  timer on a successful send; sub-threshold ticks keep re-probing.

Env vars (add to `.env.example`):

| Var | Default | Meaning |
|---|---|---|
| `PG_IIT_ALERT_INTERVAL_SECONDS` | `900` | Tick interval (min 300). |
| `PG_IIT_ALERT_AGE_MINUTES` | `10` | Minimum age for a session to count. |
| `PG_IIT_ALERT_THRESHOLD` | `2` | Sessions ≥ this count → alert. |
| `PG_IIT_TIMEOUT_MS` | `300000` | Postgres-side kill timeout (used by Fix A). |
| `ANALYZER_APP_NAME` | `unifiedanalyzer` | Populates `application_name` for triage. |

Unit test (`unifiedcollector/tests/scheduler/handlers/test_postgres_idle_txn_alert.py`):
mirror `test_bridge_unpaired_alert.py` — stub `ctx.pool.acquire()` → fake
`fetchval`, assert `should_run` throttling, assert send on threshold breach.

Optional follow-up (out of scope for this change): expose the same probe on
the dashboard `/status` panel so it shows in real time without waiting for
the alert threshold.

## Rollout

1. Land Fix A + the detection handler in one PR (analyzer-side connection.py
   + collector-side handler + `.env.example`). Restart analyzer & collector
   scheduler. Watch: an existing IIT session should be reaped within 5 min;
   `application_name` becomes visible in `pg_stat_activity`.
2. Land Fix C separately (5-line hardening, easy to review).
3. Land Fix B last; requires per-source pagination wiring and a targeted
   integration test on `github` (largest source). Do a full-resolution run
   with `EXPLAIN (ANALYZE, BUFFERS)` on the first paginated page to confirm
   keyset index usage.

Rollback: each fix is independently revertable; Fix A is a single-commit
revert on `_create_pool_once`.

## Effort estimate

| Task | Files touched | Est. dev-hours |
|---|---|---|
| Fix A: `server_settings` in `_create_pool_once` | 1 | 0.5 |
| Fix C: `except BaseException` + re-raise | 1 | 0.5 |
| Detection handler + registration + unit test + `.env.example` | 4 | 1.5 |
| Fix B: keyset pagination in `build_timeline` + integration test | 2 | 3.0 |
| Restart / verify pg_stat_activity on live DB | – | 0.5 |
| **Total** | 8 | **6.0** |

Plus 1-2 h of buffer for asyncpg pagination edge cases on the `facetracker`
UNION query (may need to stay on the cursor pattern; not a leak source since
it hits the analyzer DB, not collector).

## Confidence

| Claim | Confidence |
|---|---|
| Query attribution to `timeline_builder.py:37-44` | **1.0** — exact string, single grep hit workspace-wide. |
| Cross-pool interlock is the primary mechanism | **0.85** — code shape unambiguous; 5000-row analyzer executemany batches + `max_size=10` + concurrent pipelines is the shortest path to sustained IIT windows. |
| Cancellation-not-caught contributes | **0.6** — depends on how often the analyzer container has been restarted under load in the observed window; would rise to ~0.9 if `docker events` shows a recent restart of `unifiedanalyzer` around the observed IIT age. |
| No Postgres-side timeout | **1.0** — 0 hits for `idle_in_transaction_session_timeout` across both repos and `postgresql.conf` (grep in both). |
| Fix A alone reaps existing leaks within 5 min | **0.95** — standard Postgres behavior; only failure mode is a session in `active` state which this timeout does not cover (not the observed symptom). |
| Fix B eliminates the class of leak permanently | **0.9** — keyset pagination removes the transaction boundary that made the leak possible; residual risk is analyzer-side deadlocks unrelated to this bug. |

## Non-goals

- Not touching `_get_entity_lookup` (line 660) — it is a bounded single-batch
  fetch inside its own `async with`, releases cleanly, and never hits the
  collector pool.
- Not touching `repair_replied_metadata` (line 947+) — same pattern (`async
  with` + explicit release per batch), and it does not use a server-side
  cursor.
- Not adding `idle_session_timeout` (idle-outside-of-transaction). Real idle
  connections are pool-idle by design; killing them would churn the pool.
  Only IIT is pathological.
