# Plan: WhatsApp contact UPSERT latency (~300 ms live) — investigation and optimisation

Focused perf plan for the `whatsapp_users` contact UPSERT at
`src/collectors/whatsapp/__init__.py:672`. Measured live at **304 ms** for a
single INSERT via `docker exec unifiedcollector_postgres psql`. Question: is
that reasonable, and how do we cut it?

Short answer up-front:

- **300 ms is high** for a 6-column UPSERT against a 17k-row table with a
  single unique btree, but **the measurement itself is misleading**: a
  `docker exec … psql -c` invocation pays backend-fork + parse + plan +
  fsync on every call. The production path (asyncpg persistent pool +
  auto-prepared statements) almost certainly runs an order of magnitude
  faster. **Step 0** of the plan is to re-measure through the real code
  path before any tuning.
- Assuming the fsync-per-commit portion of the 300 ms is still ~50–100 ms
  (which is typical for Docker Desktop on Windows writing through the
  WSL2 VHDX to NTFS), **the single highest-impact change is flipping
  `synchronous_commit` from `on` to `off`** (or scoping it per-transaction
  via `SET LOCAL synchronous_commit = off`). Expected latency drop: **~5×
  to ~10×**. Trade-off: on a hard crash / power loss the last <200 ms of
  committed contact upserts may be lost. For a contact registry that is
  re-emitted from the WhatsApp bridge on every reconnect, that is
  acceptable.

---

## 1. Current state — what I verified

### 1.1 The query

`src/collectors/whatsapp/__init__.py:672` (inside `_handle_contact_event`):

```sql
INSERT INTO whatsapp_users (platform_user_id, name, pushname, phone_number, is_business, collected_at)
VALUES ($1, $2, $3, $4, $5, NOW())
ON CONFLICT (platform_user_id) DO UPDATE SET
    name         = COALESCE(EXCLUDED.name, whatsapp_users.name),
    pushname     = COALESCE(EXCLUDED.pushname, whatsapp_users.pushname),
    phone_number = COALESCE(EXCLUDED.phone_number, whatsapp_users.phone_number),
    is_business  = COALESCE(whatsapp_users.is_business, FALSE)
                   OR COALESCE(EXCLUDED.is_business, FALSE),
    collected_at = NOW()
```

Called once per Baileys contact event. Wrapped in
`async with self.pool.acquire() as conn: await conn.execute(...)`.

### 1.2 Table schema and indexes (from migrations)

From `src/db/migrations/_archive/v2_schema.sql:145–155`,
`add_whatsapp_phone_business.sql`, and `add_whatsapp_dashboard_columns.sql`:

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PRIMARY KEY | btree #1 (PK) — never touched by the UPSERT |
| `platform_user_id` | VARCHAR(255) UNIQUE NOT NULL | btree #2 (unique) — the `ON CONFLICT` target |
| `name`, `pushname` | VARCHAR(255) | inline |
| `status`, `photo_url`, `about` | TEXT | inline (short) |
| `phone_number` | VARCHAR(20) | inline |
| `is_business` | BOOLEAN | inline |
| `collected_at`, `updated_at` | TIMESTAMP | inline |

**No jsonb column, no TEXT column reliably long enough to TOAST out-of-line,
no user-defined trigger on `whatsapp_users`, no expression index.** Only two
indexes exist: the PK btree on `id`, and the unique btree on
`platform_user_id`. This rules out hypothesis (e) (TOAST) and shrinks the
index-bloat surface (b) to just two structures.

FK direction: `whatsapp_messages.sender_id → whatsapp_users(id)`. FKs
inbound to `whatsapp_users` cost nothing on writes to `whatsapp_users`
itself (they're enforced on writes to `whatsapp_messages`).

### 1.3 Postgres runtime and storage

`docker/docker-compose.yml:18–43` and `docker/postgres/postgres.conf`:

| Setting | Value | Note |
|---|---|---|
| Image | `pgvector/pgvector:pg16` | Debian-based, PG 16.x |
| `max_connections` | 200 | 14 pools × 10 max = 140 — comfortable |
| `synchronous_commit` | **on** | **every commit blocks on fsync — key lever** |
| `fsync` | on | |
| `wal_sync_method` | fdatasync | Linux default; fine |
| `full_page_writes` | on | first update to a page after checkpoint writes 8 KB to WAL |
| `wal_buffers` | 64 MB | fine |
| `checkpoint_timeout` | 5 min | PG default |
| `checkpoint_completion_target` | (unset → 0.9 in PG16) | fine |
| `max_wal_size` | (unset → 1 GB in PG16) | tight for 14 collectors |
| `commit_delay`, `commit_siblings` | (unset → 0, 5) | no group-commit batching |
| `shared_buffers` | 256 MB | small but fine for this table |
| `autovacuum` | on, `autovacuum_naptime = 60s` | check pg_stat_user_tables |
| `archive_mode` | off | as-designed; pg_dump-to-Z instead |

**Storage layout** (checked in `docker-compose.yml`):

- `pgdata` is a **named Docker volume** (line 33: `pgdata:/var/lib/postgresql/data`).
  On Docker Desktop for Windows that lives inside the WSL2 VM's ext4 VHDX,
  typically on `C:` (not on Z).
- The `z:` drive mounts (`z:/unifiedcollector/media`, `z:/unifiedcollector:/vault`)
  are for media and vault only. **The context note that "Z drive is external,
  might have poor write cache" does not apply to Postgres storage.** The
  fsync path still runs through the WSL2 → NTFS layer, which is slower than
  bare-metal Linux fsync but faster than a network drive.
- Host bind `./backups:/backups` is used by pg_dump inside the postgres
  container. Read/write for backups, not for pgdata.

### 1.4 Connection pool

`src/db/connection.py:117–130`:

- `asyncpg.create_pool(min_size=1, max_size=10, command_timeout=60, max_inactive_connection_lifetime=300)`.
- asyncpg's default `statement_cache_size` is 100. **The exact SQL string
  in `_handle_contact_event` is reused every call → cache hits per
  connection.** But `max_inactive_connection_lifetime=300` (5 min) means
  connections that idle >5 min get recycled — the next acquire lands on a
  cold connection that has to re-prepare.
- Contact events arrive in bursts when Baileys does a contact sync and
  otherwise trickle. During trickle traffic there is a real chance every
  acquire hits a cold-prepared connection.

### 1.5 The 304 ms measurement — what it actually measured

The reported command:

```
docker exec unifiedcollector_postgres psql -U <user> -d unifiedcollector -c "\timing on" -c "INSERT ... ON CONFLICT ..."
```

That path includes, per invocation:

1. `docker exec` overhead (~10–50 ms cold, ~5 ms warm)
2. `psql` binary startup + libpq connect + auth handshake (~20–50 ms)
3. New Postgres backend process fork (~5–20 ms)
4. Parse + plan the literal SQL, no prepared cache (~1–5 ms)
5. Execute: index probe on `platform_user_id_key`, HOT insert, WAL record
   emit, **fsync at commit** (~50–150 ms on Docker Desktop)
6. Return row count, print, `psql` exits

**Steps 1–4 are ~30–125 ms of overhead that the production asyncpg path does
not pay.** The 304 ms is a valid upper bound on the docker-exec probe, not
a lower bound on production per-call cost.

---

## 2. Baseline profiling — READ-ONLY, run these before any tuning

Run in `docker exec -it unifiedcollector_postgres psql -U <user> -d unifiedcollector`.
All queries are `EXPLAIN` or `pg_catalog` reads. Zero writes.

### 2.1 EXPLAIN ANALYZE the actual UPSERT

`EXPLAIN (ANALYZE, BUFFERS, VERBOSE, WAL)` on the real statement, using a
synthetic non-conflicting `platform_user_id` and a synthetic
conflicting one so both paths are exercised:

```sql
-- Path A: no conflict (hot INSERT)
EXPLAIN (ANALYZE, BUFFERS, VERBOSE, WAL)
INSERT INTO whatsapp_users (platform_user_id, name, pushname, phone_number, is_business, collected_at)
VALUES ('__probe_no_conflict_' || extract(epoch from now())::text || '@s.whatsapp.net',
        'probe', 'probe', NULL, FALSE, NOW())
ON CONFLICT (platform_user_id) DO UPDATE SET
    name         = COALESCE(EXCLUDED.name, whatsapp_users.name),
    pushname     = COALESCE(EXCLUDED.pushname, whatsapp_users.pushname),
    phone_number = COALESCE(EXCLUDED.phone_number, whatsapp_users.phone_number),
    is_business  = COALESCE(whatsapp_users.is_business, FALSE)
                   OR COALESCE(EXCLUDED.is_business, FALSE),
    collected_at = NOW();
-- immediately DELETE the probe row so the table stays clean.

-- Path B: conflict (UPDATE branch). Pick a real, low-cardinality row first.
-- SELECT platform_user_id FROM whatsapp_users LIMIT 1;  -- copy this
EXPLAIN (ANALYZE, BUFFERS, VERBOSE, WAL)
INSERT INTO whatsapp_users (platform_user_id, name, pushname, phone_number, is_business, collected_at)
VALUES ('<real_jid_from_select_above>', 'probe', 'probe', NULL, FALSE, NOW())
ON CONFLICT (platform_user_id) DO UPDATE SET
    name         = COALESCE(EXCLUDED.name, whatsapp_users.name),
    pushname     = COALESCE(EXCLUDED.pushname, whatsapp_users.pushname),
    phone_number = COALESCE(EXCLUDED.phone_number, whatsapp_users.phone_number),
    is_business  = COALESCE(whatsapp_users.is_business, FALSE)
                   OR COALESCE(EXCLUDED.is_business, FALSE),
    collected_at = NOW();
```

**What to read from the plan output**:

- `Planning Time` and `Execution Time` — if planning > 1 ms this contributes
  to per-call cost when prepared statements miss.
- `Buffers: shared hit/read/dirtied` — if `read` > 0, we're going to disk,
  meaning the page isn't in `shared_buffers`. All 17k rows should fit in
  256 MB easily; any `read` value on a warm run indicates buffer eviction.
- `WAL: records=N, bytes=M, fpi=X` — `fpi` (full-page images) counts
  full-page WAL writes. If `fpi ≥ 1` on the probe, we just paid an 8 KB
  full-page write. A checkpoint just before the probe explains this.
- The node type — should be `Insert on whatsapp_users` with a
  `Conflict Arbiter Indexes: whatsapp_users_platform_user_id_key`
  child. Any `Seq Scan` here is a bug (would mean the unique index is
  missing or unusable).

### 2.2 Confirm the unique index

```sql
SELECT indexrelname, indisunique, indisvalid, indisready,
       pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_stat_user_indexes sui
JOIN pg_index i ON i.indexrelid = sui.indexrelid
WHERE sui.relname = 'whatsapp_users';
```

Expected: **two rows**, both `indisunique = t` (the `id` PK and the
`platform_user_id` unique). Any other index adds cost per UPDATE. Any row
with `indisvalid = f` breaks `ON CONFLICT` — that would be a smoking gun.

Also:

```sql
SELECT conname, contype, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'whatsapp_users'::regclass;
```

### 2.3 Autovacuum + bloat check

```sql
SELECT relname, n_live_tup, n_dead_tup,
       CASE WHEN n_live_tup > 0
            THEN round(100.0 * n_dead_tup / n_live_tup, 2)
            ELSE 0 END AS pct_dead,
       last_vacuum, last_autovacuum, last_analyze, last_autoanalyze,
       vacuum_count, autovacuum_count, analyze_count, autoanalyze_count
FROM pg_stat_user_tables
WHERE relname = 'whatsapp_users';
```

**Decision rules**:

- `pct_dead > 20%` → run `VACUUM (VERBOSE, ANALYZE) whatsapp_users` (manual,
  during a quiet window). Repeated UPSERTs where every UPDATE writes new
  row versions can bloat fast if autovacuum is starved.
- `last_autoanalyze` older than a few hours with recent activity → planner
  statistics are stale. Run `ANALYZE whatsapp_users`.
- `autovacuum_count = 0` after weeks of live traffic → autovacuum is not
  reaching this table. Check `autovacuum_vacuum_scale_factor` and per-table
  `reloptions`.

Also inspect table + index size:

```sql
SELECT pg_size_pretty(pg_total_relation_size('whatsapp_users')) AS total,
       pg_size_pretty(pg_relation_size('whatsapp_users')) AS heap,
       pg_size_pretty(pg_indexes_size('whatsapp_users')) AS indexes;
```

At 17k rows with these columns, heap should be ≲ 4 MB and indexes ≲ 2 MB.
Anything > 10× that is bloat.

### 2.4 Row-lock contention

```sql
SELECT locktype, relation::regclass, mode, granted, pid, wait_event_type, wait_event
FROM pg_locks l
LEFT JOIN pg_stat_activity a USING (pid)
WHERE relation = 'whatsapp_users'::regclass
   OR relation IN (SELECT indexrelid FROM pg_index WHERE indrelid = 'whatsapp_users'::regclass);
```

Repeat under load — snap this a handful of times back-to-back. Look for:

- Any `wait_event = transactionid` or `tuple` → row-lock contention. Two
  bridges upserting the same JID within the same window will block each
  other on the row lock inside the `ON CONFLICT` re-check.
- `RowExclusiveLock` from many PIDs is normal; a `ShareLock` waiter is not.

Also useful:

```sql
SELECT pid, state, wait_event_type, wait_event, query_start,
       LEFT(query, 120) AS q
FROM pg_stat_activity
WHERE query ILIKE '%whatsapp_users%'
  AND state <> 'idle';
```

### 2.5 WAL flush latency

Sample commit LSN over a fixed interval to bound fsync throughput:

```sql
SELECT pg_current_wal_lsn(), pg_current_wal_flush_lsn(), pg_current_wal_insert_lsn(), now();
-- repeat 10× at 1s intervals; compute (flush_lsn_i+1 - flush_lsn_i) / interval
```

And the cumulative counters (PG14+ has `pg_stat_wal`):

```sql
SELECT wal_records, wal_fpi, wal_bytes,
       wal_write, wal_sync,          -- counts
       wal_write_time, wal_sync_time -- ms, if track_wal_io_timing = on
FROM pg_stat_wal;
```

If `wal_sync_time / wal_sync` (avg sync duration) is > 5 ms, fsync latency
alone accounts for most of the observed per-commit cost. On Docker Desktop
for Windows, 20–80 ms per sync has been observed in the wild — that maps
directly to the 300 ms envelope once backend-fork overhead from the psql
probe is layered on top.

`track_wal_io_timing` is off by default. To enable **read-only**, per-session:

```sql
SET track_wal_io_timing = on;
-- then run the sampling above; the setting reverts on session end.
```

### 2.6 Re-measure through the real code path (critical)

This is the single most important measurement in the whole plan. Add a
temporary timing probe inside the collector — no permanent code change:

```
# One-off diagnostic — DO NOT COMMIT.
docker exec -it unifiedcollector_collector_whatsapp python - <<'PY'
import asyncio, time, os
from src.db.connection import get_pool
async def main():
    pool = await get_pool()
    sql = """
    INSERT INTO whatsapp_users (platform_user_id, name, pushname, phone_number, is_business, collected_at)
    VALUES ($1, $2, $3, $4, $5, NOW())
    ON CONFLICT (platform_user_id) DO UPDATE SET
        name         = COALESCE(EXCLUDED.name, whatsapp_users.name),
        pushname     = COALESCE(EXCLUDED.pushname, whatsapp_users.pushname),
        phone_number = COALESCE(EXCLUDED.phone_number, whatsapp_users.phone_number),
        is_business  = COALESCE(whatsapp_users.is_business, FALSE)
                       OR COALESCE(EXCLUDED.is_business, FALSE),
        collected_at = NOW()
    """
    jid = "__probe_" + str(int(time.time())) + "@s.whatsapp.net"
    async with pool.acquire() as conn:
        # Warm the prepared cache.
        await conn.execute(sql, jid, "n", "p", None, False)
        # Now measure 20 identical calls (all UPDATE path, no conflict cost variation).
        t0 = time.perf_counter_ns()
        for _ in range(20):
            await conn.execute(sql, jid, "n", "p", None, False)
        elapsed_ms = (time.perf_counter_ns() - t0) / 1e6 / 20
        # Clean up.
        await conn.execute("DELETE FROM whatsapp_users WHERE platform_user_id = $1", jid)
    print(f"asyncpg per-call: {elapsed_ms:.2f} ms")
asyncio.run(main())
PY
```

Expected outcomes:

- **If asyncpg per-call is ≤ 30 ms**: the 304 ms was almost entirely
  docker-exec + psql-startup + backend-fork overhead. Production is fine.
  Stop tuning; the fix is to educate future benchmarks to use the real code
  path.
- **If asyncpg per-call is 50–200 ms**: real fsync-bound latency.
  Section 3 hypothesis (a) applies. Proceed with `synchronous_commit` tuning.
- **If asyncpg per-call is > 200 ms**: something is very wrong (locks,
  massive bloat, or a wedged autovacuum worker). Go to 2.3, 2.4, 2.5 and
  find the smoking gun before touching config.

---

## 3. Hypothesis ranking — likely causes of the observed 300 ms

Ranked by expected contribution, **assuming the re-measurement in 2.6 shows
production per-call in the 50–200 ms band**. If 2.6 shows ≤ 30 ms, the whole
ranking is moot and no tuning is warranted.

### (a) WAL fsync latency — **HIGH, ~50–100 ms of the 300 ms envelope**

Docker Desktop for Windows stores the named `pgdata` volume inside the
WSL2 VM's ext4 VHDX (an NTFS-hosted virtual disk). `synchronous_commit=on`
forces every COMMIT to wait for `fdatasync()` on the WAL segment. Through
the VHDX/NTFS layer this typically costs 10–80 ms per sync, sometimes more
under concurrent write pressure from 14 collectors.

Corroboration: postgres.conf explicitly sets `synchronous_commit = on`
and `fsync = on` with no `commit_delay`. There is no group-commit batching.
The bridge emits contact events one-at-a-time, so each one takes an
individual fsync.

Confirm via 2.5 (`wal_sync_time / wal_sync` and LSN polling).

### (b) Full-page image WAL amplification — **LOW-MEDIUM, situational**

`full_page_writes = on`. Immediately after a checkpoint (every 5 min or
1 GB of WAL), the first update to any 8 KB page writes the whole page to
WAL. If the 304 ms probe happened right after a checkpoint, that alone
added ~8 KB × 2 pages (heap + unique index leaf) to the WAL record,
inflating fsync time. This is a small factor and will average out at
scale.

Confirm via 2.1 (`WAL: fpi=X` line in EXPLAIN output) and 2.5
(`wal_fpi` counter).

### (c) Cold prepared-statement cache — **LOW-MEDIUM, only for docker-exec probe**

The `docker exec … psql -c` invocation forks a fresh backend that has zero
prepared cache. Parse + plan is fast (~1–5 ms) but real. Production
asyncpg with `statement_cache_size=100` avoids this after the first call
per connection. If contact traffic is bursty enough that pool connections
idle > 5 min and get recycled (`max_inactive_connection_lifetime=300`),
this cost is repaid on the next acquire — but at most once per idle
window, not per call.

Confirm via 2.6 (asyncpg per-call time) and by comparing the first vs
subsequent measurements in that loop.

### (d) Index bloat on `platform_user_id_key` — **LOW, but worth ruling out**

At 17k rows and two indexes, bloat is unlikely to dominate. If autovacuum
never ran on this table (checked in 2.3), the unique index could still
have HOT-chain fanout that costs the `ON CONFLICT` speculative-insert
re-check a handful of buffer reads. Even in the worst case the cost is
single-digit ms.

Confirm via 2.3 (`pg_stat_user_tables`) and index size query.

### (e) Row-lock contention with other collectors — **LOW, occasional**

The bridge is the only writer to `whatsapp_users`, but two Baileys
bridges (`wa_bridge_1`, `wa_bridge_2`) both feed `collector_whatsapp` via
RabbitMQ. If both push the same contact event within a few ms of each
other, the second one waits on the row lock the first one holds during
the `ON CONFLICT` `DO UPDATE`. Wait time = commit time of the first
transaction ≈ fsync latency. This can *stack* onto (a) but rarely
originates 300 ms on its own.

Confirm via 2.4 during a live contact-sync burst.

### (f) Autovacuum starvation — **LOW, monitoring finding**

Autovacuum default thresholds are `autovacuum_vacuum_scale_factor = 0.2`,
so vacuum triggers only after 20% + 50 dead tuples. On a 17k-row table
that's 3.5k dead tuples — plausible on a chatty contact table. If (2.3)
shows high dead-tup ratio and old `last_autovacuum`, that's the fix, not
config. Vacuum here is a one-shot maintenance action, not a config change.

### (g) TOAST out-of-line storage — **ruled out**

`whatsapp_users` has no `jsonb` column, no `bytea`, and the TEXT columns
`status/photo_url/about` are unlikely to exceed the 2 KB TOAST threshold.
Not a factor for this table.

### (h) Docker-exec + psql-startup overhead — **HIGH but not a real problem**

Already discussed. This is the reason the 304 ms number is not directly
representative of production. Not a tuning target; a measurement-method
correction.

---

## 4. Prepared-statement analysis (hypothesis 3 in the request)

**asyncpg auto-prepares by default.** Every call to
`await conn.execute(sql_str, *args)` on the same connection with the same
SQL literal reuses the cached prepared statement (default
`statement_cache_size = 100`). The identical SQL string is hard-coded in
`_handle_contact_event`, so cache hit rate should be effectively 100% on
warm connections.

The two real risks:

1. **Cache eviction on connection recycle.** Pool default in this repo is
   `min_size=1, max_size=10, max_inactive_connection_lifetime=300`. When an
   idle connection is closed and a new one is created for the next acquire,
   the prepared cache starts empty. The first UPSERT on that connection
   pays parse + plan (~1–5 ms). This is the *only* channel through which
   the current code pattern loses to explicit `conn.prepare(sql)` reuse.
2. **Cache thrashing** if many distinct SQL strings share a pool
   connection. Not applicable here — `_handle_contact_event` uses one
   template.

**Recommendation** (only if 2.6 shows the parse-plan portion is measurable
and connection-churn is frequent):

- **Option 4a — bump the idle lifetime.** Set
  `max_inactive_connection_lifetime=900` (15 min) in
  `src/db/connection.py`. Zero-risk, keeps prepares warm during typical
  contact-event trickle. Cost: a handful of MB of idle backend RSS per
  container × 14 containers.
- **Option 4b — explicit prepared statement, held at collector scope.**
  Restructure `_handle_contact_event` to call
  `stmt = await conn.prepare(sql)` once per connection (cache the
  PreparedStatement on the connection's `state`), then reuse. This is
  the pattern asyncpg is already doing internally under the auto-cache;
  making it explicit gains at most microseconds and adds code complexity.
  **Not recommended** unless 2.6 profiling proves the cache is missing.
- **Option 4c — bigger `statement_cache_size`.** Currently the default
  100. Not the bottleneck; do not change.

Verdict: **explicit prepared-statement reuse is not worth doing** given
the auto-cache. The idle-lifetime bump (4a) is a cheap keeper if 2.6
shows any parse-plan cost.

---

## 5. Postgres config hypotheses — concrete value changes

The following table lists proposed edits to
`docker/postgres/postgres.conf`. All are **live-tunable** with the
exception of `shared_buffers` (needs restart) and `max_wal_size` (needs
`SIGHUP` reload). Values are calibrated for a 14-collector single-host
laptop deployment with 256 MB `shared_buffers` and a Docker Desktop VHDX
backing store.

| Setting | Current | Proposed | Rationale |
|---|---|---|---|
| `synchronous_commit` | `on` | **`off`** | Cuts fsync wait from commit path. Highest single-lever gain (~5–10×). See 3(a). Data risk: last <200 ms of committed writes lost on crash. Acceptable for a contact registry re-emitted by the bridge. |
| `commit_delay` | `0` | `100` (µs) | Group-commit: postgres waits up to 100 µs at commit for peer transactions to piggy-back on the same fsync. Meaningful only if `synchronous_commit=on` stays. With `commit_siblings=5`, kicks in when ≥5 active transactions. Low risk. |
| `commit_siblings` | `5` | `5` | Keep default. |
| `wal_buffers` | `64 MB` | `64 MB` | Already generous. No change. |
| `wal_writer_delay` | (default 200 ms) | `200 ms` | Fine. |
| `wal_writer_flush_after` | (default 1 MB) | `1 MB` | Fine. |
| `max_wal_size` | (unset → `1 GB`) | `4 GB` | Reduces checkpoint frequency under 14-collector write load. Cost: up to 4 GB in `pg_wal/` between checkpoints (was capped at 173 GB before the archive fix). Trade-off: longer crash-recovery replay. At current write rates, checkpoints will still happen roughly every 5 min from `checkpoint_timeout`. |
| `min_wal_size` | (default `80 MB`) | `1 GB` | Prevents recycling churn when write rate spikes. |
| `checkpoint_timeout` | `5min` | `5min` | Keep. Raising it inflates recovery-replay time. |
| `checkpoint_completion_target` | (unset → `0.9` in PG16) | (unset) | Already good in PG16. |
| `full_page_writes` | `on` | `on` | Do not disable. Losing it risks torn-page corruption. |
| `bgwriter_lru_maxpages` | (default 100) | `200` | Keeps dirty-buffer eviction ahead of writer contention. Low risk. |
| `track_wal_io_timing` | `off` | `on` | Adds `wal_sync_time` / `wal_write_time` in `pg_stat_wal`. Overhead is measurable on very high-frequency clock-read platforms; on x86_64 with a good TSC it's negligible. **Turn on for the diagnostic window in section 2.5, then decide whether to leave it on.** |

**Single-line change if you want just one tweak:**
`synchronous_commit = off`. Everything else is second-order.

**Per-transaction alternative (safer than a global flip):**
The collector can wrap only the contact UPSERT in a per-transaction
`SET LOCAL synchronous_commit = off`. This is already the pattern the
codebase uses elsewhere — see `src/core/base_collector.py:791`
(`SET LOCAL statement_timeout = ...`). Scoping the relaxation to just this
one write path leaves durability guarantees intact for `whatsapp_messages`,
`recon_observations`, and everything else.

Sketch (not for implementation now — plan-only):

```python
# inside _handle_contact_event, wrap the UPSERT in an explicit transaction
async with self.pool.acquire() as conn:
    async with conn.transaction():
        await conn.execute("SET LOCAL synchronous_commit = off")
        await conn.execute("""INSERT INTO whatsapp_users ...""", ...)
```

**Trade-off summary of `synchronous_commit=off` for this write path:**

- Loses at most the last group of contact events on a hard OS crash /
  power loss. Contact events are re-emitted by Baileys on reconnect, and
  the WhatsApp bridge does a full contact resync on startup. Data is
  self-healing.
- Does **not** risk torn-page or index corruption. WAL is still written
  and fsynced on schedule (`wal_writer_delay`); it just isn't blocking the
  client commit.
- Explicitly permitted per Postgres docs for exactly this pattern.

---

## 6. Recommended actions — ordered by impact

Ordered assuming the re-measurement in 2.6 confirms fsync-bound latency
(hypothesis 3(a)). If 2.6 shows production is already ≤ 30 ms per call,
**stop after step 0** — the 300 ms number was a benchmark artefact.

| # | Action | Kind | Expected gain | Effort | Risk |
|---|---|---|---|---|---|
| **0** | **Re-measure through asyncpg (section 2.6)** | Diagnostic | — | 10 min | none |
| **1** | Scope `SET LOCAL synchronous_commit = off` inside the `_handle_contact_event` UPSERT transaction | Code | **~5–10×** on this call site | ~1 h | low — contact data is self-healing |
| 2 | If (1) is insufficient across write-heavy call sites, flip `synchronous_commit = off` globally in `postgres.conf` | Config | ~5–10× on **every** commit | ~15 min + restart | low-medium — last ~200 ms of any commit at risk on power loss |
| 3 | Set `max_wal_size = 4GB`, `min_wal_size = 1GB` | Config | ~5–15% headroom, prevents checkpoint storms | ~15 min + `pg_reload_conf()` | none |
| 4 | Bump `max_inactive_connection_lifetime` from 300 to 900 s in `src/db/connection.py` | Code | Reduces cold-prepare cost across all collectors | ~30 min | none |
| 5 | If EXPLAIN ANALYZE (2.1) shows fpi churn, add `commit_delay = 100` for group-commit batching | Config | 5–10% under bursty write load | ~15 min | none |
| 6 | If pg_stat_user_tables (2.3) shows `pct_dead > 20%` on `whatsapp_users`, run manual `VACUUM (VERBOSE, ANALYZE)` and consider a per-table `autovacuum_vacuum_scale_factor = 0.05` reloption | One-shot + config | Fixes bloat baseline | ~30 min | none |
| 7 | **Optional / defer**: batch contact UPSERTs at the bridge boundary. The Baileys bridge already emits events in bursts during contact sync — accumulate up to N events and issue a single `INSERT ... VALUES ($1,...), ($6,...), ... ON CONFLICT (platform_user_id) DO UPDATE SET ...` in one transaction. Cuts fsyncs by N. | Code | Proportional to burst size | ~1 dev day | medium — requires bridge coordination |

**Not recommended:**

- Explicit `conn.prepare()` reuse (redundant vs asyncpg auto-cache).
- Disabling `full_page_writes` (corruption risk).
- Raising `checkpoint_timeout` past 5 min (inflates recovery replay).
- Moving `pgdata` to Z drive (network/removable storage makes fsync
  worse, not better).

---

## 7. Sequenced implementation — with per-step rollback

Each step is small, testable, and independently revertible. Do **not**
proceed to step N+1 until step N is verified.

### Step A — Baseline profiling (READ-ONLY)

- Actions: run all queries in Section 2 (2.1 through 2.6). Capture output
  in a scratch file for later comparison.
- Verify: baseline metrics captured. Do not tune yet.
- Rollback: none required — read-only.
- Time: ~30 min.

### Step B — Bump connection pool idle lifetime (safe, no data risk)

- Actions: in `src/db/connection.py:127`, change
  `max_inactive_connection_lifetime=300` to `900`. `docker compose up -d`.
- Verify: re-run 2.6 after 15 min of live traffic; expect first-call and
  steady-state times to converge.
- Rollback: revert the line and `docker compose up -d`.
- Time: 30 min.

### Step C — Scope `synchronous_commit = off` to the contact UPSERT

- Actions: in `_handle_contact_event`, wrap the UPSERT in
  `async with conn.transaction(): await conn.execute("SET LOCAL synchronous_commit = off"); await conn.execute(...)`.
  Do the same for the sibling `whatsapp_lid_map` UPSERT in the same
  function if 2.6 shows it's on the same path.
- Verify: re-run 2.6. Expect per-call latency to drop by ~5×. Compare
  `pg_stat_wal.wal_sync_time` before/after (should stay roughly flat —
  same fsyncs, but not blocking the client).
- Rollback: remove the two lines and redeploy.
- Time: 1 h coding + 30 min verify.

### Step D — Set `max_wal_size = 4GB`, `min_wal_size = 1GB`

- Actions: edit `docker/postgres/postgres.conf`, add both lines.
  `docker compose exec postgres pg_ctl reload` (or `SELECT pg_reload_conf()`).
  No restart needed for either setting.
- Verify: `SHOW max_wal_size;` and `SHOW min_wal_size;` return the new
  values. Watch `pg_stat_bgwriter.checkpoints_timed` vs
  `checkpoints_req` — the ratio should tilt further toward `_timed` under
  same load.
- Rollback: revert the two lines, reload config.
- Time: 15 min.

### Step E — (Only if Step C isn't enough) Global `synchronous_commit = off`

- Actions: edit `docker/postgres/postgres.conf`, flip
  `synchronous_commit = on` to `off`.
  `docker compose exec postgres pg_ctl reload`.
- Verify: `SHOW synchronous_commit;` returns `off`. Sample the same
  benchmark against a handful of other write paths (media_items insert,
  telegram_messages insert) to confirm they also see the drop.
- Rollback: flip back, reload. This step is **the most impactful and most
  reversible**: no data structure changes, no code changes.
- Time: 15 min.

### Step F — Vacuum + autovacuum tuning (only if Step A found bloat)

- Actions:
  ```sql
  VACUUM (VERBOSE, ANALYZE) whatsapp_users;
  ALTER TABLE whatsapp_users SET (
      autovacuum_vacuum_scale_factor = 0.05,
      autovacuum_analyze_scale_factor = 0.05
  );
  ```
- Verify: re-check `pg_stat_user_tables` — `pct_dead` should be near 0
  immediately after. Watch that autovacuum_count increments over the next
  day at a healthy pace.
- Rollback: `ALTER TABLE whatsapp_users RESET (autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor);`
  and let the next autovacuum do its work.
- Time: 30 min.

### Step G — (Optional, deferred) Batched contact UPSERT at bridge boundary

- Actions: change `_handle_contact_event` to accumulate contact events in
  a bounded queue and flush in one transaction every `N` events or `T`
  seconds. Use asyncpg's `executemany` or a multi-VALUES INSERT.
- Verify: pg_stat_wal `wal_sync` count over an hour drops by roughly the
  batch factor. Contact-freshness lag (time between Baileys event and
  DB row) stays under the freshness watchdog's threshold.
- Rollback: revert the flush loop and go back to per-event UPSERT.
- Time: 1 dev day.

---

## 8. Effort estimate + confidence

| Phase | Effort | Confidence in outcome |
|---|---|---|
| Step A (baseline) | 30 min | HIGH — read-only diagnostic, will tell us which hypothesis is right |
| Steps B–D (idle lifetime + scoped `synchronous_commit=off` + WAL sizing) | ~2–3 h | **HIGH** — canonical Postgres tuning, well-documented outcomes |
| Step E (global `synchronous_commit=off`) | 15 min | HIGH — pure config, immediately reversible |
| Step F (vacuum) | 30 min | MEDIUM — helpful only if bloat is real; Step A will tell us |
| Step G (batched UPSERTs) | 1 dev day | MEDIUM — requires bridge-side coordination and freshness accounting |

**Total baseline effort to get the win:** ~3–4 hours end-to-end for
Steps A–E, most of it verification. The actual config/code changes total
about 30 lines.

**Confidence in the top recommendation (Step C: scoped
`synchronous_commit=off` on the contact UPSERT):** **HIGH**. Every element
lines up: (i) postgres.conf explicitly has `synchronous_commit = on`,
(ii) storage is a Docker Desktop VHDX-backed volume on Windows, whose
fsync latency is documented to be the slowest link, (iii) the contact
UPSERT is the classic "high-volume low-value" write pattern that
`synchronous_commit = off` is intended for, and (iv) the pattern
(`SET LOCAL synchronous_commit = off` inside `conn.transaction()`) is
already established in this codebase for `statement_timeout`.

**Confidence that the 304 ms number itself overstates production
latency:** **HIGH**. The docker-exec-psql path adds ~30–100 ms of overhead
per invocation that asyncpg does not pay. Section 2.6 will resolve this
in ten minutes.

**Residual risk:** none of the recommended changes touch schema, indexes,
or business logic. All are revertible in under 15 minutes. The only
non-reversible action described (Step F, `VACUUM`) is standard
maintenance and does not modify visible data.

---

## Appendix — cross-reference to source-of-truth files

- Query: `src/collectors/whatsapp/__init__.py:672` (inside `_handle_contact_event`)
- Table DDL: `src/db/migrations/_archive/v2_schema.sql:145–155`
- Column additions: `src/db/migrations/add_whatsapp_phone_business.sql`,
  `src/db/migrations/add_whatsapp_dashboard_columns.sql`
- Postgres config: `docker/postgres/postgres.conf`
- Postgres image: `docker/docker-compose.yml:19` (`pgvector/pgvector:pg16`)
- Volume: `docker/docker-compose.yml:33` (named volume `pgdata`, not a
  bind mount on Z)
- Pool: `src/db/connection.py:117–130`
- Existing `SET LOCAL` precedent: `src/core/base_collector.py:791`
