# Collector Audit & Throughput Tracking

> **Purpose:** Living tracking doc for the unifiedcollector audit. Structured so any LLM agent can pick up mid-task.
> **Created:** 2026-06-12 by orchestration run (Fable lead + Sonnet subagents).
> **Status legend:** ✅ done · 🟡 in progress · ⏳ blocked/pending · ❌ failed

---

## MEMORY/CPU CUTS + STRAVA PORT + JSONB SWEEP (2026-06-14, batch A/B/C)

### A — memory/CPU cuts (docker-compose.yml + worker + main)
- **#5 merge (supersedes #1):** `collector_github` + `collector_strava` +
  `collector_search` → ONE `collector_lowrisk` container, `--source github,strava,search`,
  `mem_limit: 768m` (was 1300+512+512 = 2324m). `src/main.py` now splits
  `--source` on commas so several low-volume sources share one worker process
  (one interpreter RSS baseline instead of three). ~1.5 GB reclaimed.
- **#3 idle main `collector`:** `mem_limit 384m → 128m` (all sources disabled;
  it only idles as image-builder/safety-net).
- **#2 tor removed:** verified unused — `TOR_PROXY_ENABLED` set nowhere
  (`tor_proxy.is_enabled()` always False), `WEBSITE_USE_TOR=false`,
  `INSTA_PROXY_DISABLED=true`, and `SEARCH_TOR_PROXY` has **zero code references**
  (dead env var). Deleted the `tor` service + all `depends_on: tor` entries.
- **#4 CPU cadence:** worker inter-cycle sleep was hardcoded `300s`. Added
  `Worker._cycle_sleep(source)` → env `COLLECTOR_CYCLE_SLEEP_<SOURCE>` /
  `COLLECTOR_CYCLE_SLEEP_SECONDS` (floor 30s). Set github/strava=900s, search=600s
  (on collector_lowrisk), website=600s. Fewer wakeups, lower steady-state CPU.
- Net: ~1.7–1.9 GB headroom → directly relieves the rabbitmq memory-watermark alarm.
- Trade-off accepted: github/strava/search lose per-source container fault
  isolation (low ban-risk, rarely wedge — the safe candidates).

### B — Strava privacy-zone truncation forward port (#1) — COMPLETE
The forward path in `_collect_gps_streams` was already ~95% ported (stream_status
incomplete/truncated_empty/ok, `_is_truncated`, derive start/end from first/last
non-null `latlng`, truncation points). Ported the remaining nuance from
`archive/stravatoolkit/ingestion/transform.py`: the **truncated_empty** case
(API returns an empty latlng stream object → fully privacy-hidden activity) now
flags `privacy_zone_start=True` + keeps the summary start as the truncation point.
feature_gap_analysis.md's "#1 unported" was stale (pre-dated 7ef42b1/65b6916).
Deleted the 5 `debug_strava_*.py` scratch scripts from the repo root.

### C — jsonb silent-failure sweep — REAL BUG FOUND + FIXED
- **telegram_reaction_counts + telegram_polls were failing on EVERY insert.**
  `_tg_json` is the `json.dumps(default=...)` *callback*, not a serializer —
  but it was called directly: `_tg_json(counts)` → `str(dict)` → single-quoted
  Python repr → **invalid JSON** → the `$N::jsonb` cast raised → swallowed at
  `logger.debug`. Added `_tg_jsonb()` (proper `json.dumps(obj, default=_tg_json)`)
  and fixed all 4 call sites (reactions ×2, poll options, poll vote_counts).
- **Instagram `_upsert_post` swallows:** the two callers logged at DEBUG
  (invisible in prod — how the posts=0 bug hid). Raised to `logger.warning(..., exc_info=True)`.
- Swept the other `::jsonb` sites — all correctly serialized: strava `metadata_json`,
  website `_json.dumps(images)`, youtube `json.dumps(extra_meta)`. instagram
  `relationships` / `profile_photo_history` inserts have **no jsonb column** (all
  scalar) so the audit's worry there doesn't apply.

---

## 10-TASK SWEEP + COLLECTION_SPEC TIER AUDIT (2026-06-14)

Ran the 10 open tasks (2 ops + Phase 0 + cross-cutting + 6 tier phases) against
the live code. Result: most of the spec was already implemented; two real gaps
were found and fixed, one matrix cell corrected. All 12 collectors are `Up
(healthy)` throughout. Details below — structured for mid-task handoff.

### Task 1 — stale health-alert dashboard URL :8002 → :8700 — ✅ already clean
No `:8002` exists anywhere in the repo. The dashboard is uniformly `:8700`
(`docker/docker-compose.yml:563`, `docker/Dockerfile.dashboard:12`,
`dashboard/frontend/vite.config.ts:10-11`, `src/dashboard/api.py:26`). No
external alert/prometheus/alertmanager config exists. Fixed in a prior commit;
nothing to change.

### Task 2 — transient beeper DNS / name-resolution blips — ✅ FIXED (code)
Root: a transient DNS blip (e.g. the local `pihole` resolver restarting — it was
`unhealthy` at audit time) made `host.docker.internal` momentarily unresolvable.
`BeeperClient._get`/`serve_asset` wrapped every `httpx.HTTPError` into
`BeeperAPIError`, which `collect()` logged at **ERROR** + bumped the cycle error
count, with **no retry** — exactly the log-spam the graceful-offline policy
forbids.
**Change** (`src/collectors/beeper/__init__.py`): new `BeeperTransientError`
subclass + `_is_transient_network_error()` classifier (getaddrinfo / name-
resolution / connect-timeout markers); new `_request()` helper retries transient
blips (`BEEPER_TRANSIENT_RETRIES=3`, backoff `BEEPER_TRANSIENT_BACKOFF=1.5s`)
before raising; `collect()` + `_sync_one_chat()` treat transient errors as
"retry next cycle" (INFO/debug, `stats["transient"]`, **not** `errors`). Also
hardened `download_media` to skip malformed items instead of KeyError.
Tests: `tests/collectors/test_beeper.py` (+4 new transient cases; fixed 2 stale
fixtures that pre-dated this session — `network` key + `download_media` noop).

### Task 3 — Phase 0 shared building blocks — ✅ verified present
`BaseCollector.insert_media_item` casts `json.dumps(metadata)::jsonb` (the spec's
critical jsonb rule), sha256 dedup via `media_items UNIQUE(source,content_id)` +
DB-seeded `_known_ids`, `run_backfill()` each cycle, atomic media writes. Follow-
aware account model = `src/core/profile_access.py` (`ProfileAccessRepository` +
`SmartAccountSelector`). Helper blocks present: `link_extractor`,
`spider_discover`, `change_tracker`, `profile_photo_tracker`. **+ added two new
shared blocks this session:** `src/core/document_filter.py` (Tier 3) and
`src/core/exif_gps.py` (Tier 5).

### Task 4 — cross-cutting: tier-ordered scheduling + full backfill — ✅ (with note)
Backfill: `BaseCollector.run_backfill()` runs at the end of every `collect()`
cycle; per-collector backfill paths confirmed (strava/telegram/whatsapp/beeper/
lemon8/instagram). Tier order: the scheduler (`src/scheduler/__init__.py`) is
interval-per-source; **tier priority is honoured inside each collector** (e.g.
telegram scans stories on its own 5-min lane, instagram collects stories within
its cycle). NOTE: there is no *global* cross-source tier sequencer — acceptable
because the ephemeral 4h lanes run independently and aren't starved by media
backfill.

### Task 5 — Phase 1 Tier 1 Stories — 🟡 mostly built, 1 gap
- instagram ✅ `_collect_stories` (instaloader `get_stories`) → `story`/`story_video`.
- telegram ✅ `_scan_stories` (Telethon `GetPeerStoriesRequest`), gated by
  `TELEGRAM_STORY_SCAN_ENABLED` (5-min lane — tighter than the 4h spec, fine).
- whatsapp ✅(status) — status/broadcast JID flows in as messages.
- **tiktok ❌ — NO story code exists** despite the matrix claiming ✅. Left
  unbuilt deliberately: the audit marks tiktok the highest ban-risk collector
  ("don't push"), so a 4h story-poll lane would raise ban risk. **Matrix
  corrected ✅→🔲** for tiktok Tier 1. Build behind a default-off env gate later.

### Task 6 — Phase 2 Tier 2 Media — ✅ (with note)
HD/best-quality + media bubbles broadly present. NOTE: telegram enforces a
`_max_media_size` cap on document/media download, which deviates from the spec's
"no size cap"; kept conservative to protect disk. Revisit if full large-file
capture is required (raise/remove the cap via env).

### Task 7 — Phase 3 Tier 3 Documents & audio — ✅ FIXED (code)
**Gap:** telegram's live handler only downloaded `image/*` and `video/*`
documents (`__init__.py:961`), so **PDFs, Office files, and audio were never
collected**, and there was no executable/code skip or sticker static/animated
split.
**Change:** new `src/core/document_filter.py::classify_document()` — whitelists
PDF/Office/text/images, **skips executables + code** (exe/dll/apk/sh/py/js/…),
stores audio, keeps **static** stickers and **skips animated** (.tgs / animated
.webm), conservatively skips unknown types. Rewrote telegram `_handle_document`
to extract Telethon `DocumentAttribute*` (filename/sticker/animated/audio/video),
classify, and skip-or-download with the right `content_type`; routed **all**
documents through it (was image/video-only). Tests: `tests/core/test_document_filter.py`.
DONE 2026-07-28: WhatsApp `documentMessage`/sticker/audio bridge media and
Beeper attachments now call `classify_document` before download; executable and
code-like files are skipped, safe documents/static media continue through the
normal vault path. Tests: `tests/collectors/test_whatsapp.py` and
`tests/collectors/test_beeper.py`.

### Task 8 — Phase 4 Tier 4 Profile content — ✅ verified present
Change history (`change_tracker`/`user_change_tracker`/`profile_photo_tracker`
pHash), reactions (telegram `_enumerate_reactors_and_enqueue`), memberships +
graph spider (`SpiderDiscover` wired in instagram/lemon8/telegram/tiktok/youtube;
`graph_edges` built by the scheduler from WhatsApp co-group/DM).

### Task 9 — Phase 5 Tier 5 Location — ✅ FIXED (code) + note
**Gap:** zero EXIF-GPS extraction and no telegram/whatsapp geo-message parsing
existed, despite the matrix claiming ✅ for most platforms (only strava had
native `latlng`).
**Change:** new `src/core/exif_gps.py::extract_gps()` (Pillow + GPS IFD →
decimal lat/lon/alt, best-effort, never raises), wired into
`BaseCollector.insert_media_item` so **every** collector tags image media with
`metadata.exif_gps` when present (gated `COLLECTOR_EXIF_GPS_ENABLED`, default on).
Tests: `tests/core/test_exif_gps.py`.
_Follow-up: parse telegram/whatsapp shared/live-location MESSAGES
(`MessageMediaGeo`/venue) into structured coords — still not implemented._

### Task 10 — Phase 6 Tier 6 Everything else — ✅ verified present
Link extraction wired (whatsapp `extract_whatsapp_links`, website `_extract_links`,
telegram parse). Polls/pinned/events: telegram (matrix ✅). Links feed the spider.

### Remaining honest follow-ups (not blocking; logged for the next agent)
1. tiktok Tier-1 stories (env-gated, ban-aware).
2. telegram/whatsapp shared/live-location message → structured coords.
3. telegram media size-cap vs spec "no cap" (env decision).
4. dashboard container shows `unhealthy` though `/health` returns 200 OK on every
   probe — cosmetic/intermittent-timeout, not a collection blocker.

---

## PRIVACY-ZONE FIELDS + HEALTH/DB AUDIT + INSTAGRAM ROOT CAUSE (2026-06-13)

- ✅ **Strava privacy-zone fields** (`7ef42b1`): added `stream_status`, `privacy_zone_start/end`, `truncation_point_start/end` (idempotent schema ALTER + `_collect_gps_streams` computes via `_is_truncated`/`_haversine_m`). Historical backfill (`scripts/backfill_strava_privacy_zones.py`): 485 → `stream_status='ok'`; privacy flags left NULL where the original summary wasn't preserved in metadata (honest "unknown"). Forward path sets True/False correctly.
- ✅ **source_health false-alarm fix** (`7ef42b1`): the "no run in 14h/42h" Telegram warnings were a bug — `source_health.last_success_at` was NEVER written (only auth-pause/dead/clear touched the table). Added `_mark_source_healthy()` on every successful cycle (writes last_success_at, clears stale `dead` status e.g. lemon8). Cursor-based sources fixed; realtime sources heartbeat less often (block in run()).
- ✅ **DB audit**: the alarming `n_live_tup=0` readings were **stale autovacuum estimates**, not real (ran `ANALYZE`). Real counts healthy: telegram_messages 58,141 · tiktok_posts 5,037 / profiles 185 · strava_activities 10,594 · youtube_videos 9,636 · beeper_shadow_messages 9,962 · website_pages 5,120 · github_commits 30,642. Critical-column NULL checks all 0. **Only genuinely-empty content table: `instagram_posts`.**
- ✅ **INSTAGRAM ROOT CAUSE (posts=0)** — TWO bugs fixed:
  1. **Startup rate-limit sleep** (`e1858d8`): instagram slept up to 1h/relaunch on a stale persisted streak even though Playwright bypasses that throttle → skipped in playwright-primary mode.
  2. **Dead-session, no rotation** (`e438110`): collector always used `cookie_accounts[0]`=hongyime, whose IG session is **401 (expired)**. Session probe: hongyime=401, shotsbyseah234=401, **cchmsmediaclub/oopspwned/prawnproductions234=200**. Now rotates to a healthy account on 401 (uses existing accounts — NO credential change). _Note for user: 2 of 5 IG sessions are dead (401); 3 are alive, so collection continues._

## INSTAGRAM posts=0 — THIRD & ACTUAL ROOT CAUSE (`6434f07`)
After the startup-sleep fix and the dead-session rotation, profiles fetched fine
via cchmsmediaclub but `instagram_posts` STILL = 0. The real culprit: `_upsert_post`
passed the raw dict `node` to the jsonb `metadata` column. No dict->jsonb codec is
registered (connection.py), so asyncpg raised on EVERY insert — and the callers wrap
the upsert in `except: pass`, so it failed silently while logging "upserted 12 posts".
Fix: `json.dumps(node, default=str)`. (Three stacked bugs total: startup-sleep,
no-rotation-on-401, jsonb-metadata. All fixed.)
**Lesson/follow-up:** the bare `except: pass` around `_upsert_post` hid this for a
long time — worth replacing with a logged exception. Other jsonb inserts
(relationships ~2442, profile_photo_history ~2283) may share the dict->jsonb risk.

## STRAVA start_latlng FIX + THROUGHPUT PUSH (2026-06-13, `65b6916`/`81018f7`)
- ✅ **Strava start_latlng** — `_collect_gps_streams` now backfills start/end from the GPS track (COALESCE) when the API summary omits them (privacy-zone activities). Historical backfill ran: **258 activities fixed, 0 remaining NULL** (485/485 with streams now have coords). Forward fix deployed.
- ✅ **Throughput pushed** (env, monitor + tune down if throttled): telegram `BACKFILL_MSG_PER_SEC 20→80`, whatsapp `BACKFILL_REQ_PER_MIN 5→12` + media batch `50→100`, beeper `page_size 50→150` (new `BEEPER_PAGE_SIZE`), website `MAX_CONCURRENT_TASKS 5→10`. Early monitor: telegram FloodWait=0, no throttling observed.
- **Throughput risk tiers (advisory):** SAFE to push (self-correcting/quota-bound): telegram reads, beeper, website crawl, github (already at API ceiling, no gain). PUSH CAUTIOUSLY (ban-sensitive): whatsapp history, youtube downloads. DON'T push: tiktok (instagram-family fingerprinting, high ban risk) — left at MIN/MAX_SLEEP 0.5/2.0.

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

1. **telegram — SQLite "database is locked" (64 errs/30m)** — all 3 workers (hongyime/shotsbyseah234/oopspwned) fail `Connect failed: database is locked`; telethon keepalive crashes with `OperationalError('database is locked')`. Cause: concurrent access to Telethon `.session` SQLite files. **Fix:** give each worker/account its own session file, and/or open the session SQLite with WAL mode + a busy_timeout. File: `src/collectors/telegram/__init__.py` (session/connect path). _Note: this is also one of the `_FatalSpinLogWatcher` patterns, but at ~12/30m it's below the flood threshold (correctly not self-heal-restarting — it's intermittent, not a wedge)._
2. **lemon8 — hangs every cycle (17h stale cursor)** — watchdog: `lemon8 HUNG (no progress 1831s > 1800s)`; it slowly re-upserts the SAME posts (~40s each) and never reaches cycle-end `save_progress`. Self-heal cancels+relaunches it, but it re-hangs. **Fix:** add a per-target/per-post timeout in the lemon8 FYP-detail loop and/or raise `COLLECTOR_HANG_TIMEOUT` for lemon8; investigate why per-post takes ~40s (media download stall?). File: `src/collectors/lemon8/__init__.py`.
3. **instagram (FIXED `f861997`)** — playwright-primary was being frozen by the httpx-429 cooldown gate; gate now bypassed in playwright-primary mode. Minor follow-up: the collect() STARTUP DB-rate-limit sleep (lines ~575-606) could also be skipped in playwright-primary mode (only bites on relaunch).

### 6-task plan — EXECUTION STATUS (2026-06-13)
1. ✅ **Telegram SQLite session lock** — `busy_timeout=30000` + WAL on session conn (`b0dbcc1`). Deployed.
2. ✅ **Lemon8 false-hang** — watchdog now treats in-flight `progress_count` advancement as liveness (`b0dbcc1`). Deployed.
3. ✅ **Mode-β port to TikTok/Lemon8** — RESOLVED via Subagent 2: TikTok ALREADY has gallery-dl→yt-dlp→Playwright→API fallback (more robust than old). No port needed. Real backlog captured in `feature_gap_analysis.md` (Top 10).
4. ✅ **Backfill + spider verification** — paths exist & wired for all: strava (`_backfill_athlete_history`, `backfill_feed_history`, `get_backfill_items`, SpiderDiscover; called at startup), telegram (`TELEGRAM_BACKFILL_ENABLED=true`, `backfill_chat`, `_auto_backfill_new_accounts` @20 msg/s), whatsapp (`backfill_chat` via bridge), beeper (`get_backfill_items`). They were blocked by the DB-exhaustion + telegram-lock crashes (both now fixed), so they should run now.
5. ✅ **Telegram rate-underutilization** — root cause was the "database is locked" connect failures (now fixed), NOT pacing. Backfill pace `TELEGRAM_BACKFILL_MSG_PER_SEC=20` is reasonable; raise the env if more throughput wanted. No code change.
6. ✅ **RabbitMQ memory watermark** — raised 0.4→0.6 at runtime (`set_vm_memory_high_watermark 0.6`) for immediate headroom against publisher-blocking. _Persistence follow-up: add `vm_memory_high_watermark.relative=0.6` to a mounted `rabbitmq.conf` so it survives a rabbitmq restart (currently runtime-only)._

### Port backlog (from Subagent 2 — see `feature_gap_analysis.md`)
Highest-value: **#1 Strava GPS privacy-zone/truncation handling — likely the root cause of the `start_latlng` NULL bug** (privacy-zone activities null the summary start/end; need to derive from first/last non-null `streams.latlng` point). Then Telegram `classify_document_media`, Strava `/explore` discovery, generic link-extractor + reconciler, etc.

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
