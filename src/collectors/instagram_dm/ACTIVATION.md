# Instagram DM collector activation runbook

Runbook for flipping `src/collectors/instagram_dm/` from **scaffolding** to
**live mobile-API collector** (Option A of #39). Do not follow this without
first re-reading:

- `src/collectors/instagram_dm/README.md` — module design + isolation guarantees
- `src/collectors/instagram_dm/__init__.py::__doc__` — code-level ban-safety
  guardrails already coded in
- `credentials/instagram_dm/README.md` — credential setup

## Decision context (why this collector exists)

Instagram delivers realtime DMs over `wss://edge-chat.instagram.com/chat`
as authenticated MQTT with encoded Thrift bodies. Two paths can decode them:

  * **Option B — passive decoder from browser-observed samples.** No ban
    risk (extension only observes, never signs its own requests). Requires
    real DM samples to reverse-engineer the schema; scoped separately.

  * **Option A — this collector.** Signs its own mobile-app-style requests.
    Fully decodable output. Real ban risk. Runs isolated so a ban here can
    only kill *this* container's account.

Bryan chose to build both. Option A is scaffolded but disabled until real
mobile-API implementation lands + a dedicated throwaway account is provisioned.

## Isolation the code enforces today

- Own Docker service (`collector_instagram_dm`) under a Compose profile
  (`instagram-dm`), so `docker compose up` without `--profile instagram-dm`
  never starts it.
- Own credentials dir (`credentials/instagram_dm/`), guarded against
  accidentally pointing at `credentials/instagram/`.
- Own egress env (`INSTAGRAM_DM_PROXY_URL`), does not inherit `PROXY_URL`
  from the main IG container.
- Feature flag (`INSTAGRAM_DM_COLLECTOR_ENABLED`) default false — a boot
  with the flag off is a clean no-op log line.
- Not registered in `src/collectors/__init__.py::COLLECTORS`; the scheduler
  cannot schedule it. Runs only when the compose service directly invokes
  `python -m src.main worker --source instagram_dm` (which is what the
  compose service `command:` will do once implemented).

## What "activation" means

Activation is a sequence of three separate decisions, each reversible:

  1. **Code activation** — implement the auth flow, MQTT client, and
     decoder in the three `NotImplementedError` methods. Merges as normal
     PRs against `main`. This alone does NOT put any traffic on IG.
  2. **Config activation** — set `INSTAGRAM_DM_COLLECTOR_ENABLED=true` in
     the container env, provide credentials, configure the proxy. This
     alone still doesn't start traffic unless step (3) has also happened.
  3. **Container start** — `docker compose --profile instagram-dm up -d
     collector_instagram_dm`. THIS is the point of no return; traffic
     hits IG the moment the container's `login()` call fires.

Each step can be reverted by unset / stop respectively.

## Pre-flight checklist (before step 3)

- [ ] Dedicated throwaway IG account exists, provisioned via mobile app,
      NOT tied to Bryan's real number/email. Two-factor set up (Meta bans
      no-2FA accounts more aggressively).
- [ ] Credentials file at `credentials/instagram_dm/<username>.txt` with
      the schema in that dir's README.
- [ ] Egress IP: verified that `INSTAGRAM_DM_PROXY_URL` resolves to a
      different exit IP than the main IG container. Residential preferred.
      Do NOT use Tor (immediate `sentry_block`).
- [ ] Postgres has the `dm_probe_log` and `dm_hook_heartbeat` tables (P1.2 /
      P1.3 telemetry) — you'll want them to compare mobile-API-sourced rows
      against the extension-observed baseline.
- [ ] Watchdog is running (`docker ps | grep watchdog`). The
      `source_health` row for `instagram_dm` will surface `challenge_required`
      as `degraded`.

## Post-launch monitoring (first 72h)

Meta ban decisions typically surface within 72h of the first authed
request from a new device fingerprint. Watch for:

  * `source_health.status = 'degraded'` for `instagram_dm` with a
    last_error mentioning `challenge_required`, `checkpoint_required`,
    or `sentry_block`. If any appears: **stop the container immediately**.
    `docker stop unifiedcollector_collector_instagram_dm`. Further
    requests will confirm the ban and burn the IP.
  * `dm_hook_heartbeat` for `platform='instagram'` — if the main IG
    collector's extension-observed heartbeat stops within the same
    window, that's cross-contamination. Stop both, investigate.

## Rollback

To fully disable and preserve the option to re-enable later:

```powershell
# Stop the container (no more traffic).
docker stop unifiedcollector_collector_instagram_dm
# Flag it off in env.
# In .env or docker-compose.yml: INSTAGRAM_DM_COLLECTOR_ENABLED=false
# Container will boot clean and log the disabled-by-flag line.
```

The DB rows already collected by this collector stay intact (they have
`ingest_path='mobile_api'` so unifiedanalyzer can filter on provenance).

To fully remove:

- Drop the container: `docker compose --profile instagram-dm rm -f collector_instagram_dm`
- Delete credentials: `Remove-Item -Recurse credentials/instagram_dm/`

The scaffolding module can stay in the repo; it costs nothing at import time
with the flag off.
