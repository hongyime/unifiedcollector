# Drain-rate acceleration — master plan

Synthesis of 4 parallel research plans. Ordered by build sequence.

## The plans

| # | Plan doc | Fix | Effort | Expected impact |
|---|---|---|---|---|
| A | `docs/plans/pool-leak-fix.md` | Idle-in-transaction reaper + keyset-pagination in analyzer's `build_timeline` | 6h | Reclaims 6+ stuck pool slots; unblocks DB contention |
| B | `docs/plans/whatsapp-contact-query-optimization.md` | `SET LOCAL synchronous_commit=off` inside txn; **plus** measure actual latency via asyncpg (not psql-exec) | 3-4h | 5-10× per-UPSERT latency drop, or none if 304ms was psql-artefact |
| C | `docs/plans/whatsapp-contact-batching.md` | `unnest($1::text[], ...)` batched UPSERT + time-window consumer | 8-10h | 20-50× sustained throughput (0.26/s → 5-15/s per consumer) |
| D | `docs/plans/whatsapp-contact-architecture-alternatives.md` | Long-term: staging-table + async merge if B+C plateau | 4-6d | Reserve. Only if B+C don't hit target |

**Total near-term effort: 17-20 dev-hours across A+B+C.**

## The most important finding (not in any single plan by itself)

**The 300ms per UPSERT that I measured live is very likely a measurement
artefact of `docker exec ... psql -c`**, which pays 30-100ms of backend
fork + libpq connect + parse-plan-fsync per invocation — costs that the
asyncpg pool amortises across its warm connections.

The **actual per-event latency inside the collector** may already be
30-50ms, in which case:
- Fix B (`synchronous_commit=off`) is a marginal 5-10ms win.
- Fix C (batching) is still the biggest lever (20-50× via round-trip
  amortisation).
- Fix A (pool leak) matters independently — 6 stuck slots is bad
  regardless of per-event latency.

**Mandatory pre-work: re-measure through asyncpg before deciding B.**
The query-optimization plan makes this Step 0. It's ~10 lines of
throwaway Python inside the collector container. Do NOT commit; just
measure and print.

## Second important finding

Architecture-alternatives plan surfaced that `_handle_contact_event`
does an **`os.fsync` to the Z: network drive** via `_archive_raw_event`
plus an audit-row INSERT via `report_raw_archive_result` — *before* the
actual DB UPSERT.

If the Z: drive fsync takes 100-200ms per event (network drive on
Windows through Docker Desktop), then:
- Fix B's `synchronous_commit=off` won't help.
- Fix C's batching still helps (fewer flushes per batch).
- But the real win is to **skip the raw-payload archive for contact
  events**, which are low-value (contact update metadata; already stored
  in the DB row itself as `pushname`, `name`, `phone_number`).

This is not in any of the 4 plans as a standalone step, but the
architecture-alternatives plan mentions it as follow-up. I recommend
adding a new small step:

**Step B0 (mandatory, before B and C):** measure `_archive_raw_event`
cost inside the collector via the same throwaway asyncpg script. If it
dominates, skip archive for `contacts.#` events by setting the target
tables to empty in `_handle_contact_event`, which short-circuits the
raw-payload path.

## Recommended build sequence

Pragmatic order optimising for "highest impact, lowest risk, earliest".

### Sprint 1 — safety-net first (~6 hours)

**A.1 (2 lines):** Add `server_settings={"idle_in_transaction_session_timeout": "300000"}`
to `_create_pool_once` in `src/db/connection.py`. Postgres auto-reaps any
IIT session >5 minutes. **Immediate leak recovery**, no application-side
change. Reversible via git revert. Ship in isolation.

**A.2 (new scheduler handler, ~50 LOC):** `PostgresIdleTxnAlertHandler` at
`src/scheduler/handlers/postgres_idle_txn_alert.py`. Same shape as
existing `BridgeUnpairedAlertHandler`. Reads `pg_stat_activity`, alerts
Telegram if IIT age exceeds `PG_IIT_ALERT_AGE_MINUTES` (default 10).
Regression-detection net.

**Not shipping (out of repo):** A.3/A.4 keyset pagination in analyzer's
`build_timeline` — that's `unifiedanalyzer/src/pipeline/timeline_builder.py`,
different repo. Deferred.

### Sprint 2 — measurement (~1 hour, mandatory)

**B.0:** Throwaway asyncpg script inside `unifiedcollector_collector_whatsapp`.
Run 1000 real UPSERTs against the live DB, print percentile latencies.
Decide the fork:
- If p50 ≥ 100ms → proceed to B.1
- If p50 < 30ms → skip B, go straight to C
- Also profile `_archive_raw_event` — if ≥50ms, add step B0.5

### Sprint 3 — conditional Postgres tuning (~2-3 hours, only if B.0 warrants)

**B.1:** Wrap `_handle_contact_event` UPSERTs in explicit `conn.transaction()`
with `SET LOCAL synchronous_commit = off`. Baileys re-emits contacts on
reconnect, so <200ms crash window is safe.

**B.2 (conditional):** If B.0 shows `_archive_raw_event` fsync is >20% of
per-event cost, short-circuit raw-payload writes for `contacts.#` events
(they're metadata, low-value archive).

### Sprint 4 — batching (~8-10 hours, the biggest structural win)

**C.1:** Add `_upsert_contacts_batch(conn, events)` helper + unit tests.

**C.2:** Refactor `_consume_contacts` to time-window buffer/flush shape
(N=`WA_CONTACT_BATCH_MAX` default 1, T=`WA_CONTACT_BATCH_TIMEOUT_MS`
default 0). Default preserves current behavior. Ships behind flag.

**C.3:** Integration test — batch vs per-event equivalence.

**C.4 (staging enable):** `WA_CONTACT_BATCH_MAX=50, TIMEOUT_MS=250`.
Config-only, 30-min soak, measure drain rate. Target: ≥5/s.

**C.5 (prod tune):** MAX=100 after 24h staging clean. 48h prod soak.

**C.6:** Docs update.

### Sprint 5 — architecture, reserve only

Skip unless B+C don't hit target. `docs/plans/whatsapp-contact-architecture-alternatives.md`
has the decision tree.

## Total expected outcome

If the DB latency is genuinely 300ms per UPSERT:
- Sprint 1 (A): recovers 6+ pool slots → moderate improvement
- Sprint 3 (B): 5-10× per-UPSERT drop → drain ~2.6/s per consumer
- Sprint 4 (C): 20-50× batch amortisation on top → 50-130/s per consumer
- **Combined: ~200-500/s across 4 consumers.** 108k backlog drains in ~4-10 min.

If DB latency is actually 30-50ms (Sprint 2 discovers this):
- Sprint 4 (C) alone: 100+/s per consumer → 400+/s → 108k drains in ~5 min.

Either way, the queue empties in **minutes not days**.

## Confidence

- Sprint 1 (A): confidence HIGH. 2-line change plus a scheduler tick pattern that already exists.
- Sprint 2 (B measurement): confidence HIGH. Throwaway Python.
- Sprint 3 (B tuning): confidence MEDIUM-HIGH. Depends on B.0 findings.
- Sprint 4 (C batching): confidence HIGH per the batching plan's own analysis.

## What I'm not doing

- Not editing the unifiedanalyzer repo (different repo, out of scope).
- Not touching Postgres server config (kept in `docker/postgres/postgres.conf`
  but changes require a postgres restart and I don't want to bounce the DB).
- Not building Sprint 5 (architecture) — that's reserve.

## Ready to build

If you say "go", I ship Sprint 1 immediately (2-line + scheduler handler),
then run Sprint 2 measurement inline, then decide B.1 vs skip based on the
number, then ship Sprint 4 in the 6 commits it wants.
