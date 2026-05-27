# Matrix Collector (Beeper) — Wave 1 Phase 0

Status: read-only proof of concept. Disabled by default.

## What it does

Wraps `src/core/matrix_client.py` (`BeeperMatrixClient`) in a thin
orchestration layer at `src/collectors/matrix.py` (`MatrixCollector`):

- `warmup()` — issues a short-timeout /sync to confirm the homeserver is
  reachable and the access_token is still valid. Returns True/False; never
  raises. Used by the scheduler to decide whether to attempt the cycle.
- `discover_rooms()` — enumerates joined rooms and logs a one-line summary
  per room (display name, member count, plain/E2EE, last activity ts).
- `collect()` — performs one /sync and persists the resulting `next_batch`
  token to the `matrix_sync_state` postgres table so the cursor survives
  restarts. **Phase 0 does NOT ingest events** — see Phase 1 below.

## Feature gate

The collector is gated behind the `MATRIX_COLLECTOR_ENABLED` env var.

- Unset / `0` / `false` / `no` / empty → disabled (default). The scheduler
  must NOT register the matrix task, and production boots fine without
  Beeper credentials.
- `1` / `true` / `yes` / `on` → enabled.

Check at runtime via `src.collectors.matrix.is_enabled()`.

## Setup steps before flipping the flag

Do all of these before setting `MATRIX_COLLECTOR_ENABLED=1`. Skipping any
of them will land you in a half-working state.

1. **Add Beeper credentials to `.env`**:

   ```
   BEEPER_HOMESERVER=https://matrix.beeper.com
   BEEPER_MATRIX_USER=@your-handle:beeper.com
   BEEPER_MATRIX_PASSWORD=<password>            # OR use BEEPER_ACCESS_TOKEN
   # BEEPER_ACCESS_TOKEN=<token>                # preferred for re-auth
   # BEEPER_DEVICE_ID=unifiedcollector          # optional, recommended
   ```

   Use a token if you already have one — that path skips the password call
   entirely (see `BeeperMatrixClient.login(access_token=...)`).

2. **Enable Online Key Backup at Beeper Desktop.**
   Settings → Security & Privacy → Secure Backup → Set up. Without this,
   E2EE rooms will return undecryptable MegolmEvents on first sync. Phase 0
   does not decrypt anyway, so this is technically only required for
   Phase 1, but you might as well do it now.

3. **Provision a persistent store path.** The matrix-nio store directory
   keeps olm/megolm session state across restarts; without it every
   restart loses keys. Default is whatever you configure when constructing
   `BeeperMatrixClient(store_path=...)`. Recommend a volume-mounted
   directory like `/data/matrix_store`.

4. **Apply the schema.** `src/db/migrations/add_matrix_sync_state_table.sql`
   creates the `matrix_sync_state` table. The scheduler's `_init_db()`
   already auto-applies anything it finds in `src/db/schemas/`, so the
   migration must either be linked into that directory or run manually
   the first time.

5. **Restart the collector.** `docker compose restart collector` (or
   equivalent) so the scheduler re-reads the env. Tail logs for
   `Matrix warmup OK for <mxid>` to confirm.

## Phase 1 (next wave) — what's still TODO

The Phase 0 orchestrator stops at "I can list rooms and bookmark a sync
cursor." Phase 1 adds the actually-useful work:

- **Event ingestion**: turn timeline events from each `SyncResponse` into
  rows in `media_items` / a new `matrix_messages` table.
- **Encrypted decryption**: recover megolm session keys from Online Key
  Backup so E2EE rooms aren't all undecryptable.
- **Media handling**: download `mxc://` attachments, dedupe via the
  existing content-hash pipeline.
- **Backfill**: paginate `client.fetch_history(room_id, ...)` to seed
  pre-existing room history, not just new messages.
- **Outbox / typing / receipts**: still out of scope — this client is
  read-only by design.

## Tests

`tests/collectors/test_matrix.py` covers the collector end-to-end against
a mocked matrix-nio (no live login). Run with:

```
python -m pytest tests/collectors/test_matrix.py -v
```
