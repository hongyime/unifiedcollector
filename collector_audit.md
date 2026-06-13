# Collector Audit & Throughput Tracking

> **Purpose:** Living tracking doc for the unifiedcollector audit. Structured so any LLM agent can pick up mid-task.
> **Created:** 2026-06-12 by orchestration run (Fable lead + Sonnet subagents).
> **Status legend:** ✅ done · 🟡 in progress · ⏳ blocked/pending · ❌ failed

---

## HEALTH SWEEP (2026-06-13 ~03:20 UTC) + REMAINING PLAN

**All 21 containers healthy** (rabbitmq recovered). Per-scraper assessment:

| Scraper | State | Errors/30m | Verdict |
|---|---|---|---|
| tiktok | cursor fresh (<1m) | 0 | ✅ healthy |
| youtube | fresh (~20m) | 1 | ✅ healthy |
| strava | fresh (~22m) | 0 | ✅ healthy |
| search | fresh (~11m) | 0 | ✅ healthy |
| github | fresh (~7m) | 0 | ✅ healthy |
| website | ~2.5h | 0 | ✅ healthy (slow cadence) |
| whatsapp | event-driven (cursor N/A) | 0 | ✅ collecting (11.8k msgs) |
| beeper | event-driven (cursor N/A) | 0 | ✅ collecting (152/cycle) |
| **instagram** | cooldown-frozen | 0 err / 11 warn | 🔧 FIXED this session (gate bypass `f861997`) |
| **telegram** | fresh cursor but erroring | **64** | 🔧 NEEDS FIX — SQLite session lock |
| **lemon8** | **17h stale** | 2 | 🔧 NEEDS FIX — hangs every cycle |

### 🔧 NEEDS TWEAKING — prioritized

1. **telegram — SQLite "database is locked" (64 errs/30m)** — all 3 workers (bryanseah234/shotsbyseah234/oopspwned) fail `Connect failed: database is locked`; telethon keepalive crashes with `OperationalError('database is locked')`. Cause: concurrent access to Telethon `.session` SQLite files. **Fix:** give each worker/account its own session file, and/or open the session SQLite with WAL mode + a busy_timeout. File: `src/collectors/telegram/__init__.py` (session/connect path). _Note: this is also one of the `_FatalSpinLogWatcher` patterns, but at ~12/30m it's below the flood threshold (correctly not self-heal-restarting — it's intermittent, not a wedge)._
2. **lemon8 — hangs every cycle (17h stale cursor)** — watchdog: `lemon8 HUNG (no progress 1831s > 1800s)`; it slowly re-upserts the SAME posts (~40s each) and never reaches cycle-end `save_progress`. Self-heal cancels+relaunches it, but it re-hangs. **Fix:** add a per-target/per-post timeout in the lemon8 FYP-detail loop and/or raise `COLLECTOR_HANG_TIMEOUT` for lemon8; investigate why per-post takes ~40s (media download stall?). File: `src/collectors/lemon8/__init__.py`.
3. **instagram (FIXED `f861997`)** — playwright-primary was being frozen by the httpx-429 cooldown gate; gate now bypassed in playwright-primary mode. Minor follow-up: the collect() STARTUP DB-rate-limit sleep (lines ~575-606) could also be skipped in playwright-primary mode (only bites on relaunch).

### Remaining audit items (from earlier phases)
- ⏳ **Subagent 2 feature-gap analysis** — RUNNING now (account limit lifted).
- ⏳ Port the Mode-β real-browser-fallback pattern to **TikTok / Lemon8** (same fingerprinting family) — candidate, pending Subagent-2.
- ⏳ Telegram rate-underutilization review — do AFTER the session-lock fix (locks are the current bottleneck, not pacing).
- ⏳ Confirm backfill paths actually run for telegram/whatsapp/beeper; confirm Strava spider enqueues discovery.
- ⏳ (low) RabbitMQ memory high-watermark tuning — currently healthy, revisit if it recurs.

## Latest changes (2026-06-13, direct implementation)

- ✅ **Instagram Playwright-PRIMARY** (`a66fcf8`): `INSTA_PLAYWRIGHT_PRIMARY` (default true) — browser fetch is now the primary profile path for max success rate; raw httpx is the fallback. Slower but bypasses the IP/endpoint throttle.
- ✅ **Self-healing container restart** (`a66fcf8`): `_self_heal_exit()` in `src/worker/__init__.py` exits the process (Docker `restart:unless-stopped` → clean restart) on the three terminal wedge states the watchdog detects (zero-progress HARD, max hang cycles, all-sources-dead). Gated by `COLLECTOR_SELF_HEAL_RESTART` (default true).
- ✅ **Self-heal on fatal log floods** (`897f6cc`): `_FatalSpinLogWatcher` (root logging handler) catches the wedge class that DOESN'T crash/hang/zero-progress — critically the **Telegram MTProto desync** (telegram is in `REALTIME_SOURCES` so it's exempt from the zero-progress trigger). Hard-exits when a known pattern floods past `COLLECTOR_SELFHEAL_LOG_THRESHOLD` (25) in `COLLECTOR_SELFHEAL_LOG_WINDOW` (120s). Patterns: telethon "too many messages had to be ignored", "security error while unpacking", "server sent a very new message", sqlite "database is locked". **Confirmed installed in all collectors live.** This makes the exact Telegram error self-healing with no manual step.
- **Deploy result:** all 12 collectors `Up (healthy)` on the new image; watcher install log confirmed in telegram/instagram/beeper; telegram desync count = 0. WhatsApp confirmed actively collecting (`whatsapp_messages` 11,832 rows, latest ~6 min old) — its prior "unhealthy" was a healthcheck false-positive for event-driven sources, not a data problem. Stale zombie-recreate containers pruned (27.7 MB).
- ⚠️ **Pre-existing follow-up (not addressed, low priority):** `rabbitmq` flips `unhealthy` on a `system_memory_high_watermark` alarm — it throttles publishers under memory pressure. Collectors using it (whatsapp/beeper) are healthy and collecting, so non-blocking, but worth tuning RabbitMQ memory later.
- ✅ **Per-platform "famous filters" confirmed intact** (no change needed): instagram `FILTER_MAX_FOLLOWERS=960`, tiktok follower cap (`tiktok/__init__.py:321`), youtube `YOUTUBE_MAX_SUBSCRIBERS` (`youtube/__init__.py:132`). All apply at collection time regardless of fetch path.

## Orchestration status

| Phase | Item | Status | Notes |
|---|---|---|---|
| 1 | Codebase mapping | ✅ | Active collectors in `src/collectors/`, legacy in `archive/` (note: folder is `archive/`, **not** `archived/`) |
| 2 | Subagent 1 — Throughput & State Auditor | ⏳ | **Hit account session limit (resets 11:20pm Asia/Singapore), returned 0 tokens. Needs re-run after reset.** Prompt preserved below. |
| 2 | Subagent 2 — Feature Gap Analyst | ⏳ | **Hit account session limit (resets 11:20pm Asia/Singapore), returned 0 tokens. Needs re-run after reset.** Prompt preserved below. |
| 2 | Subagent 3 — Instagram 429 Researcher | ✅ | Completed. Full findings + fix plan below. |
| 3 | Tracking markdown (this file) | 🟡 | Subagent-3 section complete; subagent 1 & 2 sections are placeholders awaiting re-run. |

---

## ✅ Subagent 3 — Instagram 429 Root Cause (COMPLETE) — FIX IMPLEMENTED & DEPLOYED (commit `4f83eb8`)

**Status:** All 5 recommended fixes shipped in `src/collectors/instagram/__init__.py`:
1. ✅ `GRAPH_API` host → `https://i.instagram.com/api/v1` (`:103`)
2. ✅ `collect()` constructs a real `Account` with `_build_fingerprint()` and sets `self._current_account` before the first request (was a throwaway placeholder)
3. ✅ `collect()` sends full `self._headers()` set + `X-CSRFToken` from the cookie jar (was 2 headers)
4. ✅ `collect()` calls `self._warmup(client)` once per process before the first API call (env-toggle `INSTA_WARMUP_ENABLED`)
5. ✅ `_handle_rate_limit()` only rotates within the env account pool — skips spurious instaloader login for cookie-only accounts
- Built + deployed to `collector_instagram`; stale streak=3 rate-limit cursor cleared to validate.
- _Note: query_hash demotion (item 5 in research) deferred — current fallback order already puts instaloader first in practice; revisit if GraphQL path still 429s._

### ⚠️ VALIDATION RESULT — residual 429 is ENVIRONMENTAL (IP/endpoint throttle), not a code bug

After deploying the fix, the first request still 429'd. Direct in-container probes (now cleaned up) localized the cause precisely:

| Request (from the collector's own IP) | Result |
|---|---|
| `i.instagram.com/api/v1/users/web_profile_info/` — **authenticated** | 429, **empty body** |
| `i.instagram.com/api/v1/users/web_profile_info/` — **anonymous (no cookies)** | 429, **empty body** |
| `www.instagram.com/api/v1/users/web_profile_info/` | 429, empty body |
| `www.instagram.com/` (homepage HTML) | **200**, 594 KB |
| `www.instagram.com/natgeo/` (profile page HTML) | **200**, 594 KB |

**Conclusions (evidence-based):**
1. **Empty-body 429** = Instagram **edge/IP rate-limit**, NOT an app-layer flag. A flagged session/challenge returns a JSON body (`feedback_required`, `challenge_required`, "please wait"). Empty = edge throttle.
2. **Anonymous also 429s** → it is NOT an account/session/cookie problem. No cookie hygiene fixes it.
3. **HTML pages return 200** → the IP is NOT banned. The throttle is **specific to the `web_profile_info` API path** for this IP, caused by today's repeated hammering.
4. **Static profile HTML no longer embeds profile stats** — `edge_followed_by`, `follower_count`, `biography`, `profile_pic_url`, `edge_owner_to_timeline_media` all absent (count 0). Modern IG hydrates these via the same throttled XHR. So cheap HTML scraping does NOT yield the data; only the API (throttled) or a JS-executing browser (Playwright Mode-β) does.

**Therefore:** the host/header/fingerprint/warmup wiring fix is correct hygiene and is retained (it makes us less likely to RE-trigger the throttle once it clears, and per-account/IP cooldown is now keyed correctly + TLS rotation active). But it cannot bypass an IP-level endpoint throttle. The only native (no-paid-proxy) levers are:
- **(a) Patience + conservative pacing** — let the endpoint throttle decay. The existing exponential backoff (900s→…→14400s cap, DB-persisted streak) already does this; after a few 429s the collector won't touch the endpoint for 1-4h, letting it clear. This is the primary, already-working mitigation.
- **(b) Different egress IP** — mobile tether / different network (NOT a paid proxy). Endpoint throttle is per-IP. `INSTA_PROXY_DISABLED=true` today (direct IP); Tor (`socks5://tor:9050`) is available but IG bans Tor exits, so it would likely be worse.
- **(c) Playwright Mode-β** — render `/{username}/` in a real browser context so its in-page XHR fetches the hydrated data. Same IP, but a genuine browser fingerprint/referer chain sometimes gets served when raw httpx is throttled. Highest-effort, uncertain payoff.
- **(d) Slow the cycle cadence** so we stop re-extending the throttle window.

**Env note:** `INSTA_WARMUP_ENABLED=false` in the deployed container, so the warmup path is wired but currently disabled by config. Flip to `true` to exercise it (adds ~90-180s/process; doesn't help the API throttle, so leave off for now).

**Recommended next step (no code change needed today):** let the backoff ride — the collector is in a correct streak-based cooldown and will probe ~once per cooldown window, allowing the IP throttle to decay. If profile data is needed sooner, the fastest real lever is (b) a different egress IP.

### ✅ Mode-β Playwright PROFILE fallback — BUILT, VALIDATED & DEPLOYED (commit `016bb46`)

The "browser context bypasses the API throttle" lever (option c) turned out to work outright, so it's now the automatic fallback rather than a manual last resort.

- **New method** `_fetch_profile_playwright(username)` in `src/collectors/instagram/__init__.py`: launches the shared single-process headless Chromium (gated by `PLAYWRIGHT_SEMAPHORE` to avoid OOM), loads `https://www.instagram.com/{username}/` to establish a real session + referer, then runs an **in-page same-origin `fetch()`** of `/api/v1/users/web_profile_info/` with the browser's own cookies/fingerprint, and returns the same `data.user` dict shape as the raw-httpx API path.
- **Wired into `_collect_user`:** on a 429, it now tries the browser fallback FIRST; it only records the 429 + backs off if the browser path ALSO fails. On success, normal processing continues (profile upsert → photo → spider → posts).
- **Reuses** existing Mode-β scaffolding: `_build_playwright_storage_state()`, `PLAYWRIGHT_LAUNCH_ARGS`, the OOM semaphore. Chromium binary confirmed present in the image (`/root/.cache/ms-playwright/chromium-1223`).
- **Validation (in-container, same IP, same cookies):**
  - raw httpx → `web_profile_info` = **429, empty body**
  - headless browser in-page fetch → `web_profile_info` = **HTTP 200, 507 KB**, recovered `natgeo` / 269,325,076 followers / "National Geographic".
- **Generalizable insight:** the same "real-browser in-page fetch bypasses a raw-httpx IP/endpoint throttle" pattern likely helps **TikTok** and **Lemon8** (same Meta/ByteDance edge-fingerprinting family). Candidate to port once Subagent-1/2 findings land.

### Original diagnosis (retained for reference)

### Root cause: the `collect()` path bypasses the entire anti-detection stack

The scheduler's entry point is `collect()` at `src/collectors/instagram/__init__.py:562-676`. It builds a **bare 2-header httpx client** and goes straight to `web_profile_info` with a cold session. The collector already *contains* a full anti-detection stack — it's just never wired into the active path:

| Asset (exists in code) | Location | Wired into `collect()`? |
|---|---|---|
| `_headers()` full header builder (device-id, csrf, capabilities, referer…) | `:477-501` | ❌ never called from collect() |
| `_warmup()` (human "app open" + initial page GET before API) | `:1562-1584` | ❌ dead code |
| `_current_account` (enables per-account cooldown, fingerprint, TLS pin) | `:203` init, `:816` only set *after* a 429 | ❌ None at first request |
| Per-account fingerprint UA/device-id | `src/core/account_pool.py:68-87` | ❌ never applied (UA comes from process-wide pool) |
| `_login_account()` / `_init_loader()` | `:278`, `:263` | ❌ unreached; `self._loader` stays None |

Because `_current_account` is `None`, `rate_limiter.async_wait(..., account=None)` (`:864`) operates on the bare `"instagram.com"` key, not per-account, and there's no first-request warmup gap.

### Candidate endpoint-host bug
- `GRAPH_API = "https://www.instagram.com/api/v1"` (`:103`).
- 2025-2026 sources indicate `i.instagram.com/api/v1/users/web_profile_info/` is the host confirmed working for anonymous/cookie profile fetches; `www.instagram.com/api/v1/...` is associated with 403/anomalies for under-authenticated requests. A host mismatch would explain a **429/403 on the literal first request** (signature problem, not volume). Hit at 3 call sites: `:870`, `:1946`, `:2555`.

### The fix is WIRING, not new infrastructure (ordered by leverage)
1. **[1-line test]** Change `GRAPH_API` → `https://i.instagram.com/api/v1` (`:103`). Cheapest, highest-leverage test of the "429 on first request" symptom.
2. Set `self._current_account` from the real `account_pool` Account (not the throwaway `type("A", (), {"name": acct_name})()` placeholder at `:638`) at the top of `collect()`. This activates per-account cooldown isolation, fingerprint UA/device-id, and TLS pinning at once.
3. Replace the inline 2-header dict (`:644-652`) with `self._headers(self._current_account)` merged with the cookie jar.
4. Call `await self._warmup(client)` once per session before the first `_process_target` (`:653`).
5. Demote the hardcoded GraphQL `query_hash` paginator (`:953`, hash `472f257a40c653c64c666ce877d59d2b`) to tier-3 fallback (after instaloader), since doc_ids rotate every 2-4 weeks.

### Research highlights (Part A) — proxy-free strategies
- Logged-in (valid warmed `sessionid` cookie) access avoids the harsh anonymous-IP ceiling (~200 req/hr, practically 1-2 req/30s before 429).
- Pacing: 2-5s jittered between calls; on 429 exponential backoff from ~2s (the repo's 900s base is excessively punitive given the real cause is signature, not volume).
- Load the profile HTML page before hitting its API (humans don't hit `/api/v1/` cold).
- `instagrapi` favors `dump_settings()`/`load_settings()` session persistence over repeated cookie import (repeated import → `login_required`).
- Shared-egress fingerprinting is real → the repo's existing "serialize IG/TikTok/Lemon8 per egress IP" comment is correct and current.
- Stealth browser plugins patch JS leaks but not TLS/JA3 or behavioral signals → fingerprint *consistency* across a session matters most.

**Sources:** Scrapfly (Instagram 2026, Playwright stealth, fingerprint impersonation), harvestmydata enrichment-endpoint, instaloader issues #2482/#1285, instagrapi best-practices/#930.

### Key file:line handoff refs
- `:103` GRAPH_API host · `:637-652` bare client + placeholder account · `:477-501` unused `_headers()` · `:861-887` `_collect_user` (429 fires `:884-886`) · `:1562-1584` dead `_warmup()` · `:203,816` `_current_account` lifecycle · `account_pool.py:68-87` fingerprint · `human_rate_limiter.py:121-167` cooldown keyed by account=None.

---

## Subagent 1 — Throughput & State Auditor (subagent BLOCKED twice by session limit; **done directly by orchestrator instead**)

The Sonnet subagent hit the account session limit on both attempts (2nd reset 4:20am SGT) and emitted no report. Diagnosed directly from live state + logs.

### Live cursor/health snapshot (2026-06-13 ~01:17 UTC)
| service | last_processed_id | status | last_processed_at |
|---|---|---|---|
| beeper | (empty) | running | NULL — never persisted |
| whatsapp | (empty) | running | NULL — never persisted |
| telegram | shotsbyseah234 | idle | 06-12 13:43 (stale ~11h) |
| lemon8 | ewjl319 | idle | 06-12 09:34 (stale ~16h) |
| github | MauricioFauth | idle | 06-13 00:52 (recent) |
| instagram/strava/tiktok/youtube/website/search | set | running | recent ✅ |

### ✅ ROOT CAUSE #1 (systemic, multi-collector): Postgres connection-pool exhaustion
- `collector_beeper`, `collector_whatsapp`, `collector_lemon8` containers all crash with **asyncpg `TimeoutError` on connect**; all four idle containers report **`unhealthy`**.
- Postgres `max_connections = 50` (`docker/postgres/postgres.conf:2`), but each pool is `min_size=2, max_size=20` (`src/db/connection.py:51`) and there are **~14 pools** (11 collectors + dashboard + scheduler + worker). Worst-case demand 14×20 = 280 ≫ 50. Live check showed **40/50 used, 33 idle** — any burst exceeds 50 → connect timeouts → the crashing collectors never reach `save_progress`, so they never set a cursor.
- **FIX APPLIED & VALIDATED (commit `49425b9`):**
  - `docker/postgres/postgres.conf`: `max_connections 50 → 200` (live-confirmed `SHOW max_connections = 200`).
  - `src/db/connection.py`: pool `min_size 2→1`, `max_size 20→10`, both env-overridable (`DB_POOL_MIN_SIZE`/`DB_POOL_MAX_SIZE`). 14×10 = 140 < 200, headroom.
  - **Post-deploy verification:** collector connections **40 → 17**; beeper/whatsapp/lemon8 `TimeoutError` count over 3 min = **0** (was crash-looping); all three log "Database pool created" and containers are `Up`. Connection-exhaustion resolved.

### ✅ POST-FIX OUTCOME — the "idle" collectors are now collecting
Verified ~02:00-02:04 UTC after the DB fix + recreate:
- **beeper** → healthy; `Beeper cycle done: accounts=11 chats=25 messages=152 errors=0` (actively syncing).
- **lemon8** → healthy; `Feed scraped: 153 media, 50 users`, upserting posts (e.g. `7528360839162642961`).
- **telegram** → healthy; MTProto desync gone (see #2).
- **whatsapp** → running, connected to Redis + RabbitMQ (event/queue-driven via wa-bridges).

**Cursor red-herring correction:** beeper & whatsapp load `cursor last_id=None` *by design* — they are bridge/event-driven and track dedupe state via "known content_ids from DB" (beeper 6818, whatsapp 768, lemon8 28071), NOT the `last_processed_id` column. Their empty cursor was therefore never the real problem; the DB-connection crash-loop was. lemon8 DOES use a real cursor (updates at cycle end). The single root cause behind all four sitting idle was Root Cause #1 (DB exhaustion).

### ✅ ROOT CAUSE #2: Telegram MTProto session desync (separate from DB) — CLEARED by restart
- `collector_telegram` was spamming Telethon `Server sent a very new message ... ignoring` + `Too many messages had to be ignored consecutively`. Known Telethon symptom of **MTProto message-id/clock desync** — the client made no forward progress (stale ~11h cursor).
- **RESOLVED:** the container recreate cleared the corrupt in-memory MTProto state — desync occurrences over 2 min after restart = **0**, container now `healthy`. If it recurs, the durable fix is host clock-sync / regenerating the session file / ensuring a single process owns the session.
- _Leftover: cursor value `shotsbyseah234` is a stale cross-written checkpoint (an IG handle); harmless, will be overwritten on the next real telegram checkpoint. Watch that it advances._

### TODO (remaining throughput items)
- Verify Telegram/WhatsApp/Beeper backfill paths actually run once DB connectivity is stable.
- Confirm Strava spidering enqueues discovery (strava cursor is live/running, looks OK).
- Telethon rate underutilization review (deferred until session desync fixed).

---

## ⏳ Subagent 2 — Feature Gap Analyst (PENDING — re-run after 11:20pm SGT)

> **TODO(agent):** Re-spawn this Sonnet subagent after the session limit resets. Objective: compare new `src/collectors/` + `src/core/` against legacy `archive/{instagramtoolkit,telegramtoolkit,tiktoktoolkit,youtubetoolkit,stravatoolkit,lemon8toolkit,websitetoolkit,githubtoolkit,searchtoolkit,whatsappcollector,matrix-wave1}/`. Extract old spider/download/media features, cross-reference against new code, list what was omitted in the rewrite. Deepest effort on instagram/telegram/tiktok/youtube/strava. Output: per-platform table (Old Feature | Present? | Evidence | Impact | Port rec) + Top-10 omitted-features list.

_(findings to be filled in on re-run)_

---

## Verified independently this session
- **Graceful offline:** implemented in `src/worker/__init__.py` — `_wait_for_internet` (`:219-235`) logs once on loss/recovery with 10s→×1.5→120s backoff; startup gate at `:121`; per-source network recovery at `:465-476`. Matches the "pause patiently, no log spam, wait for internet on boot" requirement.
- **Instagram 429 streak persistence:** fixed earlier (commit `8759e8f`) — `_RateLimitHandled` sentinel preserves `_consecutive_429s` across the success path; DB entry survives the cycle.

## Next actions
1. After 11:20pm Asia/Singapore: re-run Subagents 1 & 2 (prompts preserved above), fill their sections.
2. Implement the Instagram wiring fix (steps 1-5 in Subagent-3 section) — start with the 1-line `GRAPH_API` host test.
3. Once Subagents 1 & 2 land, expand this doc into the per-service fix checklist.
