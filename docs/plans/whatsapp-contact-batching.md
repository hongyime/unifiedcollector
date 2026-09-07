# Plan: WhatsApp contact-event batching — asyncpg + Postgres

Addresses the contact-event drain bottleneck observed 2026-09-07:

- 108,000-event backlog on `unifiedcollector.contacts` (RabbitMQ).
- Measured 300 ms wall-clock per single `_handle_contact_event` UPSERT
  against the live DB.
- Sustained drain rate 0.26 events/s in a 3-min window, i.e. the queue
  is *growing* faster than it is draining even with the 4-consumer
  concurrency that landed in commit `e88ab26e`.
- At 0.26 events/s the current backlog alone drains in ~115 hours.
  New events keep arriving, so in practice the queue never empties.

The root cause is not concurrency (we already have 4 parallel consumers,
prefetch 64, pool max 10) — it is that each event does one full DB
round-trip for a trivial ~200-byte UPSERT. Adding more consumers just
adds more coordination overhead without moving the DB-per-event lower
bound. The fix is to batch N events per round-trip.

Ground-truth references while reading this plan:

- `src/collectors/whatsapp/__init__.py::_handle_contact_event`
  (~line 604). Two UPSERTs per event (one always on `whatsapp_users`,
  one conditional on `whatsapp_lid_map` when both `lid` and phone JID
  are present).
- `src/collectors/whatsapp/__init__.py::_consume_contacts`
  (~line 446). aio-pika `queue.iterator()` loop, one message at a
  time via `async with message.process(ignore_processed=True)`.
- Table shape: `src/db/schemas/whatsapp.sql` —
  - `whatsapp_users(platform_user_id UNIQUE, name, pushname,
    phone_number, is_business, collected_at, ...)` — merge with
    COALESCE, `is_business` OR-merged.
  - `whatsapp_lid_map(lid PK, phone_jid, display_name, updated_at)` —
    merge with COALESCE on `display_name`.
- Runtime: asyncpg >= 0.31.0, aio-pika >= 10.0.1
  (`requirements.txt:1,35`).
- Pool sizing note: `src/db/connection.py:118-127` uses
  `DB_POOL_MIN_SIZE=1`, `DB_POOL_MAX_SIZE=10` by default. Collector_whatsapp
  compose override raises to 20. Either is fine for batching — one
  connection held per batch × 4 consumers = 4 conns.

---

## 1. Batching approaches evaluated

### (a1) Positional multi-row VALUES
`INSERT ... VALUES (...), (...) ON CONFLICT DO UPDATE`. Wire-protocol cap
65,535 params, so with 5 params/row = ~13k rows/statement.

### (a2) unnest arrays (RECOMMENDED)
```sql
INSERT INTO whatsapp_users (platform_user_id, ...)
SELECT t.puid, t.name, ...
FROM unnest($1::text[], $2::text[], ...) AS t(puid, name, ...)
ON CONFLICT (platform_user_id) DO UPDATE SET ...
```
Each column becomes one array param — no 65k limit. Tens of thousands of
rows per statement fine. asyncpg maps `list[str|None]` → `text[]` with NULLs.

### (b) COPY FROM STDIN
Fastest bulk insert. No ON CONFLICT support — needs staging table + MERGE.
Overkill for 100-event batches.

### (c) Temp/UNLOGGED staging + MERGE
Better than (a) when in-batch key duplicates need SQL-level dedup. Adds
~5-10ms per batch for the temp-table lifecycle. Reserve if (a) hits a limit.

### (d) Third-party wrappers
`pgcopy` is psycopg2-only. `asyncpg.pgcopy` doesn't exist. Reject.

**Summary:** pick (a2) unnest arrays.

Expected throughput: batched 100-row upsert runs in ~10-20ms wall-clock.
That's **15-30x per-batch** or **1500-3000x per-event** if batches stay full.
Conservative floor **20-50x sustained** once RabbitMQ delivery + JSON parse +
pool acquire overheads are added back.

---

## 2. Recommended implementation

**Pick (a2): unnest arrays.**

Justification:
1. Same transaction model, same ON CONFLICT semantics, same COALESCE merge.
2. asyncpg-native (Python `list[str|None]` → `text[]`).
3. No new schema, no migration file.
4. Batch size env-tunable — `WA_CONTACT_BATCH_MAX=1` reproduces current behavior.
5. `whatsapp_lid_map` upsert analogous — two batched UPSERTs per flush.

SQL shapes (illustrative):

```sql
-- Users upsert (5 array params + NOW() in SELECT projection)
INSERT INTO whatsapp_users (platform_user_id, name, pushname,
                            phone_number, is_business, collected_at)
SELECT t.puid, t.name, t.push, t.phone,
       COALESCE(t.is_biz, FALSE), NOW()
FROM unnest($1::text[], $2::text[], $3::text[],
            $4::text[], $5::bool[])
     AS t(puid, name, push, phone, is_biz)
ON CONFLICT (platform_user_id) DO UPDATE SET
    name         = COALESCE(EXCLUDED.name,     whatsapp_users.name),
    pushname     = COALESCE(EXCLUDED.pushname, whatsapp_users.pushname),
    phone_number = COALESCE(EXCLUDED.phone_number,
                            whatsapp_users.phone_number),
    is_business  = COALESCE(whatsapp_users.is_business, FALSE)
                   OR COALESCE(EXCLUDED.is_business, FALSE),
    collected_at = NOW();

-- LID-map upsert (3 array params)
INSERT INTO whatsapp_lid_map (lid, phone_jid, display_name, updated_at)
SELECT t.lid, t.jid, t.disp, NOW()
FROM unnest($1::text[], $2::text[], $3::text[])
     AS t(lid, jid, disp)
ON CONFLICT (lid) DO UPDATE SET
    phone_jid    = EXCLUDED.phone_jid,
    display_name = COALESCE(EXCLUDED.display_name,
                            whatsapp_lid_map.display_name),
    updated_at   = NOW();
```

**In-batch dedup.** Before array-building, Python-side dedupe by
`platform_user_id` and by `lid`, keeping the *last* event's non-NULL
fields COALESCEd forward. Sidesteps "ON CONFLICT DO UPDATE cannot affect
row a second time" and is cheaper than in-SQL `DISTINCT ON`.

**Expected throughput:** target 60/s across the 4 consumers = 108k backlog
drains in ~30 minutes. Optimistic 200/s = 9 minutes.

---

## 3. Consumer batching architecture

**Pick (i) time-window batching:** accumulate up to N events OR wait T ms
since the first buffered event, whichever hits first. Flush when either
bound trips.

Alternatives rejected:
- Prefetch-driven — aio-pika gives no "drain the prefetch window" primitive.
- Pure count-only — a low-volume period would stall single events forever.

Concrete sketch (do not commit):

```python
BATCH_MAX = int(os.getenv("WA_CONTACT_BATCH_MAX", "100"))
BATCH_TIMEOUT_MS = int(os.getenv("WA_CONTACT_BATCH_TIMEOUT_MS", "250"))

async def _consume_contacts():
    async with contact_queue.iterator(no_ack=False) as qi:
        buffer: list[tuple[aio_pika.IncomingMessage, dict]] = []
        deadline_ms: float | None = None
        while not self._stop.is_set():
            timeout = None
            if buffer:
                remaining_ms = BATCH_TIMEOUT_MS - (
                    monotonic() * 1000 - deadline_ms)
                timeout = max(0.001, remaining_ms / 1000)
            try:
                message = await asyncio.wait_for(qi.__anext__(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._flush_contact_batch(buffer)
                buffer.clear(); deadline_ms = None
                continue
            try:
                body = json.loads(message.body.decode())
            except Exception:
                await message.reject(requeue=False)
                continue
            buffer.append((message, body))
            if deadline_ms is None:
                deadline_ms = monotonic() * 1000
            if len(buffer) >= BATCH_MAX:
                await self._flush_contact_batch(buffer)
                buffer.clear(); deadline_ms = None
        if buffer:
            await self._flush_contact_batch(buffer)
```

**CRITICAL:** no `async with message.process(...)` — that auto-acks BEFORE
the DB commit. Batch consumer manages ack/nack explicitly after commit.

---

## 4. Failure semantics

**Pick (i) per-message fallback on batch failure:**

- Contact events are idempotent under ON CONFLICT DO UPDATE. Reprocessing
  a committed event is a no-op (modulo `collected_at`).
- Alternative (ii) "skip the poison row and retry rest" is hard: Postgres
  doesn't reliably report the offending row index across asyncpg.
- Alternative (iii) "nack whole batch and rely on RabbitMQ retry" risks
  infinite loop on a genuinely malformed payload.

Pattern:
```python
async def _flush_contact_batch(buffer):
    if not buffer: return
    try:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self._upsert_contacts_batch(conn, buffer)
        for msg, _ in buffer:
            await msg.ack()
    except Exception as e:
        logger.warning("Contact batch of %d failed (%s); falling back per-msg",
                       len(buffer), e)
        for msg, body in buffer:
            try:
                async with asyncio.timeout(_handler_timeout):
                    await self._handle_contact_event(body)
                await msg.ack()
            except Exception:
                await msg.reject(requeue=False)
```

---

## 5. Ack safety

Rule: `message.ack()` must happen strictly AFTER `conn.transaction()` exits
without error.

- Postgres commits first (data safe).
- Un-acked messages will be redelivered on next consumer startup.
- Redelivery re-runs UPSERT with COALESCE → no data loss, no duplicates,
  small cost of one extra UPSERT per redelivered event.

**Do NOT** use `message.ack(multiple=True)` — 4 parallel consumers share
one channel; multiple-ack would ack another consumer's in-flight messages.

**Do NOT** use `async with message.process(...)` — that auto-acks on
context exit, before your commit.

---

## 6. Sequenced implementation steps

**Step 1.** `feat(whatsapp): add _upsert_contacts_batch() helper`
- New function reads a list of events, dedupes in Python by
  `platform_user_id` and by `lid`, builds unnest arrays, issues both UPSERTs.
- No consumer wiring. `_handle_contact_event` unchanged.
- Gate: unit tests in `tests/collectors/test_whatsapp_contact_batch.py`.

**Step 2.** `feat(whatsapp): batching consumer behind WA_CONTACT_BATCH_MAX (default 1)`
- Rewrite `_consume_contacts` to buffer/flush shape.
- Default MAX=1, TIMEOUT_MS=0 → behaviorally identical to today.
- Gate: existing tests pass. Live-deploy + 30-min log tail confirms no
  drop in drain rate at MAX=1.

**Step 3.** `test(whatsapp): integration test — batch vs per-event equivalence`
- Fixture of ~50 realistic events, assert end-state identical whether
  processed via batch or sequential.
- Gate: CI green.

**Step 4.** `chore(whatsapp): enable batching in staging (MAX=50, TIMEOUT_MS=250)`
- Config-only. Measure drain rate over 30 min.
- Gate: measured rate ≥ 20× baseline (0.26 → ≥5/s).

**Step 5.** `chore(whatsapp): raise production batch max to 100`
- After 24-hour staging soak with no data loss, no unbounded memory,
  no pool exhaustion.
- Gate: 48-hour production soak.

**Step 6.** `docs(whatsapp): document WA_CONTACT_BATCH_* env vars`

Rollback at any step: Steps 1-3 via `git revert`. Steps 4-5 via env
`WA_CONTACT_BATCH_MAX=1` restart.

---

## 7. Effort estimate

**8-10 active dev-hours. Confidence: HIGH.**

| Step | Hours | Notes |
|---|---|---|
| 1. helper + unit tests | 3-4 | Bulk of code |
| 2. consumer refactor | 2 | Replace `async for` body |
| 3. integration test | 1-2 | Extends existing DB test pattern |
| 4. staging enable + measure | 0.5 + 30 min soak | Config only |
| 5. prod tune | 0.5 + 24-48h soak | Config only |
| 6. docs | 1 | Env-var rows + measured outcomes |

Risks: in-batch key duplicates (mitigated by Python-side dedup), pool
contention (headroom fine at 4 conns × 20 max), memory (100 events × 1KB
= 100KB negligible), ack-storm (individual acks), poison payload (fallback
per-message).

---

## Appendix — reject-reasons for tempting alternatives

- **"Just add more consumers."** Already at 4 (commit `e88ab26e`); to hit
  20/s that way would need ~300 consumers exhausting `max_connections=200`.
- **"Just raise pool size."** Same reason — bottleneck is per-event RTT,
  not connection availability.
- **"Use LISTEN/NOTIFY."** Events originate from Baileys over AMQP, not
  Postgres. LISTEN doesn't batch.
- **"Skip UPSERT for events without new info."** Multiplies the win from
  batching, doesn't replace it. Consider as a follow-up.
