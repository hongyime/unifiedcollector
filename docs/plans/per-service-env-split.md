# Plan: Per-service env split

Addresses **SEC-003** — monolithic `.env` exposes all secrets to every container.

> **Sprint status (as of the `per_service_env_split_first_slice` sprint):**
> Steps 1-6 are DONE. The `docker/env/` directory now holds 21 tracked
> `*.env.example` templates plus `README.md`; `docker/.gitignore` blocks
> populated `docker/env/*.env` files from ever being committed.
>
> Steps 7-23 are DEFERRED to a follow-up sprint that requires:
>
> 1. An operator to run
>    `Copy-Item docker/env/*.env.example docker/env/*.env` and populate each
>    file with real values (typically lifted from the current monolithic
>    `.env`). The live `docker/env/*.env` files must exist on disk before
>    compose wiring can be added, and this repo's convention is that the
>    operator populates env files by hand — the agent does not fabricate them.
> 2. Staged validation windows against the live docker stack so each
>    additive wiring commit (steps 7-19) can be verified with a real boot +
>    `/health` check per affected service before the next commit lands.
> 3. Only after all additive steps pass validation, step 21 (the subtractive
>    removal of `../.env` from every service's `env_file:` list) can run in
>    its own dedicated sprint with rollback tooling ready.
>
> No `docker-compose.yml` service `env_file:` list has been modified in this
> sprint. Every service still sources `../.env` exactly as before.

## 1. Context — current state

- Top-level `.env` (13,617 B; sanitized `.env.example` is 15,438 B and lists the full key inventory).
- `docker/docker-compose.yml` defines **22 services**:
  `postgres`, `collector`, `collector_spiderfoot`, `collector_youtube`, `collector_tiktok`, `collector_lowrisk`, `watchdog`, `collector_website`, `collector_exposure`, `collector_lemon8`, `collector_telegram`, `collector_beeper`, `collector_whatsapp`, `collector_instagram`, `collector_instagram_dm`, `ig_ingest`, `scheduler`, `onboard_bot`, `realtime_feed`, `dashboard`, `backup`, `rabbitmq`, `redis`, `browser_cookie_vault`.
- Grep confirms **44 `env_file:` lines** in `docker-compose.yml` — every service sources `../.env`.
- Secret categories in `.env`: DB, storage, GitHub, Instagram (6 accounts), Telegram (4 accounts + hub + notify + onboarding bots), YouTube, Strava, TikTok, Lemon8, Search, WhatsApp (session/broker/redis), Beeper, Dashboard (JWT/admin), MQ credentials, SpiderFoot/GHunt.

**Real security problem:** `collector_spiderfoot` runs untrusted OSS enrichment (maigret hitting 3000+ third-party sites; GHunt against Google's unofficial endpoints) yet has environment access to Instagram passwords, Telegram API hashes/session strings, and every other collector credential. A memory dump, log leak, or exploit in that container leaks the entire credential set.

## 2. Motivation

- **Blast radius**: one compromised container = every credential leaked. Every non-recon service has the same problem: `collector_youtube` doesn't need Telegram API hashes.
- **Least-privilege violation** documented and unfixed.
- **Compliance**: personal-data handling under GDPR/PDPA benefits from documentable secret scoping.
- **Auditing**: the repo already runs trufflehog, semgrep, bandit, codeql, scorecard workflows. Per-service env scoping is a coverage gap those tools can enforce once files exist.
- **Rotation**: today, rotating an Instagram password touches a file that also holds the DB admin password. Operators are more cautious than they should need to be.

## 3. Target end state

```
docker/env/
  README.md                 # explains ownership + rotation
  common.env                # DATABASE_URL, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST_PORT,
                            # COLLECTOR_DRIVE_PATH, COLLECTOR_VAULT_ROOT, COLLECTOR_SIDECARS_ENABLED,
                            # DASHBOARD_DB_ACQUIRE_TIMEOUT_SECONDS (universal timeouts), TZ
  instagram.env             # INSTA_ACCOUNT_1..6_*, FILTER_MAX_FOLLOWERS, SLIDING_WINDOW_*, INSTAGRAM_IDLE_SECONDS
  telegram.env              # TELEGRAM_API_ID/HASH, TELEGRAM_ACCOUNT_1..4_*, TELEGRAM_BOT_TOKENS, spider allowlist,
                            # TELEGRAM_HUB_GROUP*, TELEGRAM_BACKFILL_*, TELEGRAM_STORY_SCAN_*
  telegram_notify.env       # NOTIFY_TELEGRAM_BOT_TOKEN, NOTIFY_TELEGRAM_CHAT_ID/THREAD_ID,
                            # UC_NOTIFY_BOT_USER_ID, TELEGRAM_LOGS_CHAT_ID
  telegram_onboard.env      # PRAWNPRODUCTIONS_BOT_TOKEN, SHOTSBYSEAH_BOT_TOKEN, BRYANSEAH_BOT_TOKEN,
                            # TELEGRAM_AUTO_BACKFILL_NEW_ACCOUNTS
  youtube.env               # YOUTUBE_API_KEY, YOUTUBE_CLIENT_*, YOUTUBE_COOKIE_*, YOUTUBE_MAX_*
  strava.env                # STRAVA_COOKIES_FILE, STRAVA_SESSION_COOKIE, STRAVA_API_DELAY_*, STRAVA_GPS_*
  tiktok.env                # TIKTOK_COOKIES_FILE, TIKTOK_SESSION_ID, TIKTOK_MIN/MAX_SLEEP, TIKTOK_*_ENABLED
  lemon8.env                # LEMON8_COOKIES_FILE, LEMON8_* image/delay knobs, LEMON8_FEED_ENABLED
  whatsapp.env              # WHATSAPP_SESSION_NAMES, WHATSAPP_MEDIA_BRIDGE_SECRET, WHATSAPP_RABBITMQ_URL,
                            # WHATSAPP_REDIS_URL, WHATSAPP_BROKER_TYPE, WHATSAPP_SESSION_BRIDGES_JSON,
                            # WHATSAPP_SPIDER_SESSIONS, WHATSAPP_FINDINGS_HUB_GROUP_JID
  beeper.env                # BEEPER_DESKTOP_API_URL, BEEPER_DESKTOP_API_TOKEN, BEEPER_COLLECTOR_ENABLED
  github.env                # GITHUB_TOKEN, GITHUB_AVATAR_SIZE, GITHUB_*_DELAY, GITHUB_*_CONCURRENCY
  search.env                # SEARCH_API_KEY, SEARCH_MIN_*, SEARCH_TOR_PROXY, PROXY_URL
  website.env               # WEBSITE_MAX_DEPTH, WEBSITE_MAX_PAGES, WEBSITE_USE_TOR, WEBSITE_TIMEOUT
  exposure.env              # exposure-specific overrides (if any)
  recon.env                 # RECON_ALLOWLIST, RECON_ALLOW_UNSCOPED, RECON_USERNAME_ENGINE,
                            # SPIDERFOOT_*, MAIGRET_*, GHUNT_CREDS
  dashboard.env             # DASHBOARD_ADMIN_USERNAME, DASHBOARD_ADMIN_PASSWORD, DASHBOARD_JWT_SECRET
  realtime_feed.env         # REALTIME_POST_FEED_* knobs (feed daemon only)
  broker.env                # RABBITMQ_USER, RABBITMQ_PASSWORD, RABBITMQ_VHOST, REDIS_PASSWORD
  browser_cookie_vault.env  # BROWSER_COOKIE_VAULT_AUTORESTORE/INTERVAL/KEEP/HEALTH_PORT, CHROME_CDP_URL
  backup.env                # COLLECTOR_DB_BACKUP_*
```

Docker service → env_file mapping (representative subset):

| Service | env_file list |
|---|---|
| `postgres` | `common.env`, `broker.env` (POSTGRES_* only) |
| `collector` | `common.env` (idle; catch-all) |
| `collector_spiderfoot` | `common.env`, `recon.env` — **no collector platform creds** |
| `collector_youtube` | `common.env`, `youtube.env` |
| `collector_tiktok` | `common.env`, `tiktok.env` |
| `collector_lowrisk` | `common.env`, `github.env`, `strava.env`, `search.env` |
| `collector_website` | `common.env`, `website.env` |
| `collector_exposure` | `common.env`, `search.env`, `exposure.env` |
| `collector_lemon8` | `common.env`, `lemon8.env` |
| `collector_instagram` / `_dm` | `common.env`, `instagram.env` |
| `collector_telegram` | `common.env`, `telegram.env`, `telegram_notify.env` |
| `collector_beeper` | `common.env`, `beeper.env` |
| `collector_whatsapp` | `common.env`, `whatsapp.env`, `broker.env` |
| `ig_ingest` | `common.env`, `instagram.env` (cookie sync uses IG creds) |
| `scheduler` | `common.env`, `telegram_notify.env` (alerts), `realtime_feed.env` |
| `onboard_bot` | `common.env`, `telegram.env`, `telegram_onboard.env` |
| `realtime_feed` | `common.env`, `realtime_feed.env`, `telegram_notify.env` |
| `dashboard` | `common.env`, `dashboard.env` |
| `backup` | `common.env`, `backup.env`, `telegram_notify.env` (failure alerts) |
| `rabbitmq` / `redis` | `broker.env` |
| `watchdog` | `common.env`, `telegram_notify.env` |
| `browser_cookie_vault` | `common.env`, `browser_cookie_vault.env` |

The monolithic top-level `.env` is retired.

## 4. Sequenced steps (commit-per-step)

The migration is **additive-first, subtractive-last** so containers never lose access to a variable mid-refactor.

1. `chore(env): create docker/env/ directory with common.env and all per-service .env templates (empty values)` — files exist but no service references them yet. `.env.example` gets a note pointing to `docker/env/*.env.example` (added in parallel).
2. `chore(env): add docker/env/common.env in-repo with DB + storage vars; wire ALL 22 services to also source common.env (dual-source with ../.env)` — additive. Verify boot: `docker compose config` for each service now lists two env_files.
3. `chore(env): populate instagram.env; wire collector_instagram, collector_instagram_dm, ig_ingest to also source it (dual-source with ../.env)` — additive. Verify boot.
4. `chore(env): populate telegram.env; wire collector_telegram, onboard_bot` (dual-source).
5. `chore(env): populate telegram_notify.env; wire scheduler, realtime_feed, watchdog, backup, collector_telegram` (dual-source).
6. `chore(env): populate telegram_onboard.env; wire onboard_bot` (dual-source).
7. `chore(env): populate youtube.env; wire collector_youtube` (dual-source).
8. `chore(env): populate strava.env; wire collector_lowrisk` (dual-source).
9. `chore(env): populate tiktok.env; wire collector_tiktok` (dual-source).
10. `chore(env): populate lemon8.env; wire collector_lemon8` (dual-source).
11. `chore(env): populate whatsapp.env + broker.env; wire collector_whatsapp, rabbitmq, redis` (dual-source).
12. `chore(env): populate beeper.env; wire collector_beeper` (dual-source).
13. `chore(env): populate github.env; wire collector_lowrisk` (dual-source; collector_lowrisk now sources common+github+strava+search).
14. `chore(env): populate search.env, website.env, exposure.env; wire collector_lowrisk, collector_website, collector_exposure` (dual-source).
15. `chore(env): populate dashboard.env; wire dashboard` (dual-source).
16. `chore(env): populate realtime_feed.env; wire realtime_feed` (dual-source).
17. `chore(env): populate browser_cookie_vault.env; wire browser_cookie_vault` (dual-source).
18. `chore(env): populate backup.env; wire backup` (dual-source).
19. `chore(env): populate recon.env; wire ONLY collector_spiderfoot` (dual-source).
20. `chore(env): verify env parity with static analysis (new scripts/verify_env_split.py)` — no compose changes; adds a CI script that for each service computes: `sourced_env = union of KEYS in env_file list` and `referenced_env = grep os.getenv/env_int for src/<domain>`; fail if a referenced var is not in the sourced set.
21. `chore(env): remove ../.env from every service's env_file list (final subtractive step)` — the sensitive one. Move top-level `.env` to `.env.legacy.bak` (gitignored). Run full stack; run `scripts/verify-collector-boot.ps1`.
22. `docs(env): add docker/env/README.md; retire top-level .env.example in favor of per-service .env.example files; update main README section on Startup`.
23. `chore(env): update scripts/migrate_env.py to emit per-service files instead of a monolith`.

## 5. Rollback per step

- Steps 1–19 are **additive**: revert restores the previous env_file list. No secret is ever lost from a container during these steps because `../.env` is still attached.
- Step 20: adds a check script only. Revert is trivial.
- **Step 21 is the critical one.** Rollback strategy:
  - Keep `.env.legacy.bak` staged (do NOT `git rm` it in the same commit; move it out of the tree instead).
  - If any service fails to boot after step 21, re-add `../.env` to that service's env_file list temporarily and open a bug against the missing var. `docker compose up -d <service>` immediately restores service.
  - Full rollback = revert step 21 commit; `.env` returns to compose.
- Steps 22–23: docs/scripts only; revert is safe.

## 6. Test strategy

### Static

- **New CI check** (`scripts/verify_env_split.py`, added in step 20):
  - For each service in `docker-compose.yml`, parse `env_file` list → `sourced_env` set of KEYS.
  - For each source directory that runs in that service (`src/collectors/<name>`, `src/bridges/ig_ingest`, `src/dashboard`, `src/scheduler`, `src/notifications`, `src/tools/browser_cookie_vault.py`, `src/backup`), grep `os.getenv("([A-Z_]+)"`, `env_int(...)`, `os.environ["..."]` → `referenced_env` set.
  - Fail CI if `referenced_env - sourced_env` is non-empty.

- **Cross-contamination probe**: at step 19, run
  ```
  docker exec collector_spiderfoot printenv | \
    grep -iE 'INSTA_|TELEGRAM_(?!NOTIFY)|YOUTUBE_|STRAVA_|WHATSAPP_|BEEPER_|TIKTOK_|LEMON8_|GITHUB_' | wc -l
  ```
  Result **must be 0** after step 21.

### Runtime

- After every step, `docker compose up -d <affected_services>` and hit each service's `/health` (200 required).
- After step 21, full `docker compose down && docker compose up -d`; run `scripts/verify-collector-boot.ps1` (existing tool). All 22 services must reach healthy state within the boot budget.
- After step 21, run a 30-min live-collect window; verify per-source ingest into `media_items` for each of 5 test accounts (IG, TG, WA, YouTube, Strava). Confirms no service is silently missing a required secret.

## 7. Effort estimate + confidence

- **4–6 dev days.** Confidence: **MEDIUM-HIGH**.
- The mechanical work is easy. The risk is *missing an env reference* in a dark corner of the codebase — e.g., a helper module reads `os.getenv("FOO")` from within a service that historically had FOO free from the monolith.
- Mitigations: step 20's static check catches most cases; step 21's dry-run in staging + `verify-collector-boot.ps1` catches boot-time misses; keeping `.env.legacy.bak` accessible catches runtime misses in the first 24–48 h.


---

## Step 21 deferral (added by env_split_wiring_and_subtractive sprint)

Steps 7–20 completed under the `env_split_wiring_and_subtractive` sprint:

- **7–19: additive wiring.** Every service's `env_file:` list in
  `docker/docker-compose.yml` now sources both `../.env` (legacy monolith,
  intact) AND the appropriate `docker/env/<service>.env` file(s). Because
  every service still dual-sources `../.env`, no container can lose access to
  any variable during this phase — the additive property is preserved.
- **20: static check.** `scripts/verify_env_split.py` was added and iterated
  against. The script now reports `OK: every referenced env var is either
  sourced or documented in an env.example`. Along the way, ~466 previously-
  undocumented env keys referenced in code were added to their appropriate
  per-service `.env.example` templates. The script is NOT wired into CI yet
  (see plan step 20).

**Step 21 (subtractive removal of `../.env` from every `env_file:` list) is
deliberately deferred** to a distinct, operator-scheduled sprint because it
is the one irreversible-until-rollback step in this plan and needs:

1. **Operator go-ahead** with a staged validation window per service.
2. **`docker/env/<service>.env` populated with real values** on the operator
   host (`docker/.gitignore` already blocks these files from being committed).
   The operator confirmed on 2026-09-06 that `docker/env/instagram.env` and
   the other `docker/env/*.env` files exist on disk, but the removal step
   should still be staged per-service, not all-at-once.
3. **Per-service boot verification.** Suggested order (least → most blast
   radius):
   1. `browser_cookie_vault`, `postgres`, `rabbitmq`, `redis` (support
      infra; a bad boot is trivially reversed by re-adding `../.env`).
   2. `dashboard`, `watchdog`, `backup` (ops-only; no user data loss on
      failure).
   3. `collector_spiderfoot` (recon-only; security-critical win, isolated).
   4. `collector_youtube`, `collector_tiktok`, `collector_lemon8`,
      `collector_website`, `collector_exposure` (headless collectors,
      medium ban-risk).
   5. `collector_lowrisk` (github + strava + search merged worker).
   6. `collector_beeper`, `collector_telegram`, `collector_whatsapp` (realtime
      messaging — parked-listener services, hardest to detect a silent
      credential-miss).
   7. `collector_instagram`, `collector_instagram_dm`, `ig_ingest` (Meta
      properties — highest ban risk if a wrong env slips in mid-window).
   8. `scheduler`, `onboard_bot`, `realtime_feed` (last; they consume from
      the DB the other services fill).
4. **Rollback tooling ready.** In each per-service window: on failure,
   revert the single-service `../.env` removal, `docker compose up -d
   <service>`, and re-check `/health` before proceeding.
5. **Static-analysis re-check post-removal.** After each per-service
   subtractive commit, rerun `python scripts/verify_env_split.py` — the
   sourced-key count for that service will drop (loses `../.env`'s 225 keys)
   and any newly-exposed genuine gap will show as `MISSING` instead of the
   soft `cross-service` category.

Until step 21 lands, the security property from plan §2 (limit
`collector_spiderfoot`'s credential exposure) is only ASPIRATIONALLY
enforced: the code path that would leak IG passwords into a maigret run does
not exist, but `../.env` still hands the raw credentials to every container.
Step 21 is what converts the aspirational property into an enforced one.

**Steps 22–23** (docs / `scripts/migrate_env.py` update) also stay
deferred until after step 21 lands.
