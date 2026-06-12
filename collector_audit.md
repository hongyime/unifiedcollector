# Collector Audit & Throughput Tracking

> **Purpose:** Living tracking doc for the unifiedcollector audit. Structured so any LLM agent can pick up mid-task.
> **Created:** 2026-06-12 by orchestration run (Fable lead + Sonnet subagents).
> **Status legend:** ✅ done · 🟡 in progress · ⏳ blocked/pending · ❌ failed

---

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

## ⏳ Subagent 1 — Throughput & State Auditor (PENDING — re-run after 11:20pm SGT)

> **TODO(agent):** Re-spawn this Sonnet subagent after the session limit resets. Objective: diagnose why WhatsApp, Beeper, Lemon8, Telegram stay idle / never set `last_processed_id`, why Telegram's rate limits are underutilized, and whether Strava spidering + Telegram/WhatsApp/Beeper backfills actually run. Key paths: `src/worker/__init__.py`, `src/core/{checkpoint,source_config,rate_limiter,human_rate_limiter,sliding_window_limiter,adaptive_rate,spider_discover}.py`, `src/collectors/{telegram,whatsapp,beeper,lemon8,strava}/__init__.py`, `migrations/` + `docker/` for the `service_cursors` schema. DB: postgres `unifiedcollector`, user `collector`. Output: per-service root cause with file:line + concrete fix.

_(findings to be filled in on re-run)_

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
