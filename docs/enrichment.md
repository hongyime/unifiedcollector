# Enrichment pipeline — deep reference

This document covers the collector-side enrichment subsystem in depth:
database schema, scope policy, per-engine details, tuning, and operational
procedures. For a high-level orientation see the [README](../README.md#enrichment-pipeline).

## Contents

- [Design principles](#design-principles)
- [Database schema](#database-schema)
- [Scope policy](#scope-policy)
- [maigret — username fan-out](#maigret--username-fan-out)
- [FP blocklist mechanics](#fp-blocklist-mechanics)
- [Phone-OSINT — offline enrichment](#phone-osint--offline-enrichment)
- [GHunt — email to Google account](#ghunt--email-to-google-account)
- [SpiderFoot modules (domain / ip)](#spiderfoot-modules-domain--ip)
- [Stale-target reclaim](#stale-target-reclaim)
- [Analyzer bridge](#analyzer-bridge)
- [Tuning guide](#tuning-guide)
- [Observing enrichment health](#observing-enrichment-health)
- [Adding a new engine](#adding-a-new-engine)

---

## Design principles

1. **Read-only from platforms.** Enrichment never writes back to Instagram,
   Telegram, WhatsApp, or any other source. It reads collector DB rows and
   writes derived facts back into the same DB.

2. **Enrichment-only fields stay out of identity_signals.** Carrier / region
   from `wa_phone_intel` are useful context but don't identify people.
   They live in their own table and are explicitly excluded from the
   identity-signal pipeline.

3. **Policy before execution.** Every recon target is checked against the scope
   policy (allowlist + source/type gates) before any external tool runs.
   A target that fails the policy check is marked `skipped` and costs nothing.

4. **Idempotent seeding.** `recon_targets` uses `ON CONFLICT (target_type,
   target_value) DO UPDATE` so the auto-seed job can run repeatedly without
   duplicating work. A target already in progress is never reset to `pending`.

5. **Bounded by default.** `RECON_ALLOW_UNSCOPED=0` (default) means the worker
   only processes targets that came from the collector's own tables. An
   operator has to explicitly opt in to unrestricted OSINT.

6. **Weak signals only.** Observations become `identity_signals` at confidence
   ≤ 0.5. Independent hard signals are required for the analyzer to promote
   a link to `auto_truth`.

---

## Database schema

### `recon_targets` — work queue

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `target_type` | text | `domain`, `ip`, `email`, `username`, `phone`, `url` |
| `target_value` | text | Normalized (lowercased for domain/email) |
| `source` | text | `manual`, `collector:<table>`, etc. |
| `priority` | int | Lower = higher priority. `ON CONFLICT` keeps the minimum. |
| `status` | text | `pending`, `in_progress`, `done`, `skipped`, `error` |
| `scope_json` | jsonb | Per-target scope overrides (allowlist, modules, collector_source) |
| `error` | text | Last error message if status=error |
| `updated_at` | timestamptz | Updated on every status transition |

Unique constraint: `(target_type, target_value)`. The upsert on conflict sets
`priority = LEAST(existing, incoming)` so a high-priority re-seed of an existing
target is honoured.

### `recon_observations` — results

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `target_id` | uuid FK → recon_targets | |
| `module` | text | `maigret`, `spiderfoot`, `ghunt`, etc. |
| `observation_type` | text | `ACCOUNT_EXTERNAL_OWNED`, `USERNAME`, `EMAILADDR`, `HUMAN_NAME`, `INTERNET_NAME` |
| `value` | text | The discovered URL / username / email / name |
| `confidence` | float | 0.25 – 0.70; see per-engine notes |
| `raw_json` | jsonb | Full output from the engine for that observation |
| `created_at` | timestamptz | |

### `recon_maigret_fp_sites` — FP blocklist

| Column | Type | Notes |
|---|---|---|
| `site_name_lower` | text PK | Lowercased site name as maigret reports it |
| `hit_count` | int | Incremented each time this site is dropped as an FP |
| `last_refreshed_at` | timestamptz | When this row was last confirmed by a control run |

Populated by running maigret against random 16-char alphanumeric control
usernames. Sites that claim those controls are universally unreliable.

### `wa_phone_intel` — offline phone enrichment

| Column | Type | Notes |
|---|---|---|
| `phone_jid` | text PK | WhatsApp JID (digits@s.whatsapp.net or raw digits) |
| `e164` | text | Normalized E.164 format |
| `country_code` | int | Numeric country code |
| `region_code` | text | ISO 3166-1 alpha-2 (e.g. `SG`, `US`) |
| `carrier` | text | Carrier name from phonenumbers dataset |
| `line_type` | text | `MOBILE`, `FIXED_LINE`, `VOIP`, `UNKNOWN`, etc. |
| `timezones` | text[] | One or more IANA timezone names |
| `enriched_at` | timestamptz | Last enrichment timestamp |

**Never joined into `identity_signals`.** Queried directly for context panels
and operator reporting.

---

## Scope policy

The policy is evaluated in `src/core/recon_spiderfoot.py::target_allowed_by_policy()` before any tool runs. Evaluation order:

1. Does the target match the per-target `scope_json.allowlist`? **Allow.**
2. Does the target match `RECON_ALLOWLIST` (global env)? **Allow.**
3. Is `RECON_ALLOW_UNSCOPED=1`? **Allow** (unless blocked by type/source gates below).
4. Was the target seeded by a collector table (`source` starts with `collector:`)?
   - Target type must be in `RECON_COLLECTOR_TARGET_TYPES`
   - Collector source must be in `RECON_ALLOWED_SOURCES`
   - Source table must be in `RECON_ALLOWED_SOURCE_TABLES`
   - For domain/URL/email targets, the domain suffix must be in `RECON_ALLOWED_DOMAIN_SUFFIXES` (if set)
   - If all pass: **Allow.**
5. Otherwise: **Skip.**

**Intrusive modules** (`sfp_portscan_tcp`, `sfp_portscan_udp`, `sfp_dnsbrute`,
`sfp_shodan`, `sfp_censys`) are blocked by default regardless of allowlist.
Enable with `SPIDERFOOT_ALLOW_INTRUSIVE=1`.

---

## maigret — username fan-out

maigret forks an HTTP request to each site in its database for a given
username, checks the response status and body patterns, and reports `Claimed`
where the site's known-good response pattern matches. The collector runs it as
a subprocess (`maigret <username> -J ndjson -fo <tempdir> ...`) and parses the
NDJSON report directory.

**Only `Claimed` entries become observations.** Entries with status `Not
Claimed`, `Unknown`, `Illegal`, or `Available` are silently dropped.

**Confidence** is a simple heuristic applied in
`_maigret_rows_to_observations()`:

- Alexa rank 1–500: `confidence = 0.7`
- All other claimed sites: `confidence = 0.5`

The analyzer bridge caps `cross_platform_link` signals from recon at 0.5
downstream regardless; the 0.7 is preserved in `recon_observations.confidence`
for operator inspection.

**Subprocess timeout**: controlled by `MAIGRET_TARGET_TIMEOUT_SECONDS`
(default 420 s). A SIGTERM + 5 s SIGKILL sequence is sent on expiry.

**Concurrency within a run**: `MAIGRET_NUM_REQUESTS` (default 20) is the
number of concurrent HTTP requests maigret makes internally per username
invocation. Separate from the two workers, which each run one username at a time.

---

## FP blocklist mechanics

Sites that return `Claimed` for any random username (universal-200 responders)
pollute observations with noise. The blocklist eliminates them.

**Build process** (`refresh_maigret_fp_blocklist` in
`src/core/recon_spiderfoot.py`):

1. Generate `N` random 16-character alphanumeric control usernames (default N=10).
2. Run maigret sequentially against each control (sequential to keep peak RAM
   at ~1× a single maigret process).
3. Collect the union of all `Claimed` site names across all controls.
4. Upsert into `recon_maigret_fp_sites` with `ON CONFLICT DO UPDATE` — so the
   blocklist grows over time as new FP sites are discovered.

**Serialization**: a `pg_advisory_lock` on a fixed key prevents concurrent
workers from double-running the refresh. The `--force` flag uses a blocking
lock (waits for any concurrent refresh to finish); normal periodic runs skip if
the lock is held.

**Skip logic**: if `max(last_refreshed_at)` is younger than the refresh interval,
the loop sleeps until the next refresh is due rather than re-running.

**Hit tracking**: every time a site from the blocklist is encountered on a real
target, `hit_count` is incremented. High hit-count sites are the most dangerous
FPs. Inspect with:

```sql
SELECT site_name_lower, hit_count, last_refreshed_at
FROM recon_maigret_fp_sites
ORDER BY hit_count DESC
LIMIT 20;
```

---

## Phone-OSINT — offline enrichment

`src/core/wa_phone_intel.py` is an independent sweep tool, not part of the
recon-targets pipeline. It reads phone JIDs directly from `whatsapp_lid_map`
and enriches them without going through `recon_targets`.

**Batch processing**: selects up to `WA_PHONE_INTEL_BATCH` (default 100) JIDs
at a time ordered by `enriched_at ASC NULLS FIRST` — least-recently-enriched
first. Add jitter (`asyncio.sleep`) between batches to avoid DB pressure
bursts.

**Idempotent**: uses `INSERT ... ON CONFLICT (phone_jid) DO UPDATE` so
re-running the sweep on already-enriched numbers is safe.

**`phonenumbers` library**: ships with an embedded offline dataset; no network
calls, no API key, no credentials required. The dataset is updated with new
library versions via `requirements.txt`.

**Policy reminder**: `wa_phone_intel` rows **must not** be consumed by the
analyzer merge scorer or written to `identity_signals`. Enforce this by code
review on any new analyzer pipeline phases that touch phone data.

---

## GHunt — email to Google account

GHunt lives in an isolated venv (`/opt/ghunt-venv/`) baked into
`Dockerfile.spiderfoot`. The collector shells out to `ghunt email <addr> --json
<output.json>` and reads the JSON result. No Python package import — the venv
boundary ensures GHunt's dependencies don't pollute the collector's namespace.

**Credential lifecycle**:

- `ghunt login` is a one-time interactive step that writes `creds.m`.
- Credentials expire when Google rotates the session (no fixed TTL; typically
  weeks to months).
- Signs of expiry: `ghunt_enrich` returns `{"status": "error", "error": "auth
  failed"}` in the log. Re-run `ghunt login` to regenerate.
- Rotate immediately after any run that returns a Google auth error.

**No-op contract**: if `GHUNT_CREDS` is unset, or the file doesn't exist, or
the `ghunt` binary is not on PATH, `run_lookup()` returns
`{"status": "skipped", "reason": "..."}` and logs at DEBUG. No exception is
raised and no observation is written.

**Rate limiting**: Google's undocumented anti-abuse triggers are unknown. Keep
call rates low (several seconds between lookups). The collector's stale-target
reclaim handles interrupted runs.

---

## SpiderFoot modules (domain / ip)

For domain and IP targets, the recon worker invokes SpiderFoot CLI with a
configurable module set. Default modules: `sfp_dnsresolve`, `sfp_whois`,
`sfp_names`. These are passive, non-intrusive modules.

Intrusive modules (`sfp_portscan_tcp`, `sfp_portscan_udp`, `sfp_dnsbrute`,
`sfp_shodan`, `sfp_censys`) are blocked by the `allowed_modules()` filter
unless `SPIDERFOOT_ALLOW_INTRUSIVE=1`. Never enable intrusive modules on
targets you don't own or have explicit permission to scan.

SpiderFoot output is parsed from stdout JSON. Observations with module
`SpiderFoot UI` or `sfp__stor_stdout` are echo rows (the target value being
re-emitted by SpiderFoot's seed injector) and are dropped before being stored.

---

## Stale-target reclaim

Any target whose `status = 'in_progress'` and `updated_at < NOW() -
INTERVAL '<stale_minutes> minutes'` is automatically reset to `status =
'pending'` on the next worker poll. This prevents permanently stuck targets
from a crashed worker, a timeout, or a container restart mid-run.

The reclaim check runs at the top of each worker's poll loop, before claiming
a new target. It affects only the single target that was stale — it doesn't
reset the entire queue.

Configure with `SPIDERFOOT_STALE_TARGET_MINUTES` (default 30). Set lower for
fast maigret timeouts; raise if your per-target timeout is deliberately long.

---

## Analyzer bridge

The bridge (`unifiedanalyzer/src/pipeline/recon_bridge.py`) is intentionally
**opt-in** and not wired into the analyzer's automatic scheduler. Run it after
a recon batch to propagate observations:

```bash
# From the analyzer container
python -m src.pipeline.recon_bridge --dry-run   # preview: shows what would be written
python -m src.pipeline.recon_bridge             # write up to 500 observations
python -m src.pipeline.recon_bridge --limit 50  # smaller batch for testing
```

The bridge is idempotent: it checks `(source_table='recon_observations',
source_record_id=<obs_id>)` in `identity_signals` before inserting and skips
already-bridged observations. Re-running it after a partial batch is safe.

**Orphan observations** (no matching `entity_platform_link` for the target
username) are silently counted and reported in the summary but not written as
signals. They're not errors — they mean the entity hasn't been resolved yet.

---

## Tuning guide

### Throughput

Each worker processes one username target at a time (limited by maigret's
wall-clock timeout). With 2 workers and a 420-second timeout, peak throughput
is roughly 2 × (3600 / 420) ≈ 17 usernames per hour under full load.

To increase throughput:
- Raise `RECON_SPIDERFOOT_WORKERS` (each worker is a separate asyncio task
  inside one container; memory is the constraint, not CPU).
- Lower `MAIGRET_TARGET_TIMEOUT_SECONDS` if most targets finish faster and you
  prefer to skip slow ones.
- Lower `MAIGRET_TOP_SITES` to check fewer sites per target.

### Memory

Each maigret subprocess uses ~150–250 MiB during a run. With 2 workers, peak
usage is ~500 MiB plus Python overhead (~200 MiB). The default
`RECON_SPIDERFOOT_MEM=2g` gives comfortable headroom. If you raise workers to
4+, raise the memory cap proportionally.

### False positives

If you see noisy `ACCOUNT_EXTERNAL_OWNED` observations for a specific site:
1. Check `recon_maigret_fp_sites` — is the site already there?
2. If not, run a manual FP refresh: `python -m src.recon_maigret_fp_refresh --controls 10`
3. If the site consistently FP-fires, it will be added to the blocklist automatically on the next refresh cycle.
4. For immediate relief: `INSERT INTO recon_maigret_fp_sites (site_name_lower, hit_count) VALUES ('sitename', 0) ON CONFLICT DO NOTHING;`

### Scope creep prevention

Keep `RECON_ALLOW_UNSCOPED=0` (default). If you need to add a new domain to
scope, add it to `RECON_ALLOWLIST` rather than enabling unscoped mode:

```bash
# In .env:
RECON_ALLOWLIST=example.com,anotherdomain.org
```

Restart the scheduler and collector_spiderfoot to pick up the change.

---

## Observing enrichment health

### Pending queue depth

```sql
SELECT status, COUNT(*) FROM recon_targets GROUP BY status ORDER BY status;
```

A growing `pending` count with `in_progress = 0` usually means
`collector_spiderfoot` is not running (check `docker ps` and the `recon`
profile).

### Recent observations

```sql
SELECT
  rt.target_type,
  rt.target_value,
  ro.module,
  ro.observation_type,
  ro.confidence,
  ro.value,
  ro.created_at
FROM recon_observations ro
JOIN recon_targets rt ON rt.id = ro.target_id
ORDER BY ro.created_at DESC
LIMIT 50;
```

### FP blocklist status

```sql
SELECT COUNT(*) AS blocklist_size,
       MAX(last_refreshed_at) AS last_refresh,
       SUM(hit_count) AS total_hits_dropped
FROM recon_maigret_fp_sites;
```

### Phone-OSINT coverage

```sql
SELECT
  COUNT(*) FILTER (WHERE wpi.enriched_at IS NOT NULL) AS enriched,
  COUNT(*) AS total
FROM whatsapp_lid_map wlm
LEFT JOIN wa_phone_intel wpi ON wpi.phone_jid = wlm.phone_jid;
```

### Worker logs

```bash
docker logs unifiedcollector_spiderfoot --tail 100 -f
```

Look for:
- `spiderfoot status=done target_type=username observations=N modules=maigret` — healthy
- `spiderfoot status=error error=...` — target-level failure; check the error
- `fp_refresh: starting (controls=10, force=False)` — blocklist refresh kicked off
- `fp_refresh: skip (age=Xs < interval=Ys)` — blocklist fresh, skip is correct

---

## Adding a new engine

1. Implement `async def run_lookup(conn, target: dict) -> dict` in a new module
   under `src/core/`.
2. Add a routing branch in `src/core/recon_spiderfoot.py::run_spiderfoot_once()`
   to call your module when `target_type` matches.
3. Normalize output into `recon_observations` rows using the existing
   observation types (`ACCOUNT_EXTERNAL_OWNED`, `EMAILADDR`, `HUMAN_NAME`, etc.)
   or add a new type and update `recon_bridge._TYPE_MAP` in the analyzer.
4. Add a no-op contract: if the engine is not configured (missing credentials,
   binary, or env var), return `{"status": "skipped", "reason": "..."}` and
   write nothing.
5. Document the operator setup steps and policy implications (credentials,
   rate limits, ToS, privacy law).
