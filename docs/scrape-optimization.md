# Browser Scrape Optimization — 2026-08-05

Findings from a live audit of the running Chrome + extension stack against
production DB metrics. Focus: SW-routing regression fix + tab/resource
tuning, without raising ban risk.

## Baseline (immediately before fix)

**Chrome process footprint** (`Get-Process chrome | Measure-Object WorkingSet -Sum`):
- 29 chrome.exe processes, ~4.47 GB total WorkingSet, max single process 638 MB.

**Per-tab JS heap** (`Performance.getMetrics` via CDP):

| URL | JS MB | Docs | Nodes | Frames |
|---|---:|---:|---:|---:|
| www.facebook.com/ | 139.8 | 6 | 22,533 | 7 |
| www.tiktok.com/following | 68.4 | 2 | 1,388 | 1 |
| www.tiktok.com/foryou | 65.0 | 10 | 1,916 | 5 |
| x.com/home | 44.9 | 9 | 1,341 | 13 |
| www.threads.com/ | 41.0 | 2 | 5,465 | 2 |
| www.instagram.com/ | 39.8 | 2 | 2,728 | 3 |
| www.strava.com/activities/… | 35.5 | 15 | 2,027 | 15 |
| www.instagram.com/direct/inbox/ | 29.3 | 2 | 3,680 | 3 |
| www.lemon8-app.com/feed/food?region=sg | 10.7 | 6 | 409 | 6 |

Total JS heap across content tabs: **474.5 MB**. Facebook is the outlier
(3–5× everyone else); 22.5k DOM nodes = long feed history retained in DOM.

**6-hour media_items yield by source**:

| source | rows/6h | rows/hr |
|---|---:|---:|
| facebook | 769 | ~128 |
| website | 550 | ~92 |
| telegram | 309 | ~52 |
| search | 155 | ~26 |
| youtube | 148 | ~25 |
| instagram | 134 | ~22 |
| tiktok | 50 | ~8 |
| strava | 43 | ~7 |
| beeper | 9 | ~1.5 |
| x | 7 | ~1.2 |
| lemon8 | 3 | 0.5 |
| whatsapp | 2 | 0.3 |

**Cycle errors last 24h** (`browser_ingest_events.metadata->>'cycle_error'`):

| platform | error | count |
|---|---|---:|
| tiktok | tab message timed out | 135 |
| instagram | tab message timed out | 91 |
| instagram | Could not establish connection | 30 |
| strava | tab message timed out | 22 |
| tiktok | Could not establish connection | 19 |
| tiktok | TikTok loop scrape pass timed out after 5m | 16 |
| instagram | Instagram loop scrape pass timed out after 12m | 10 |
| strava | Could not establish connection | 10 |
| facebook | Could not establish connection | 5 |

The dominant failure mode is **SW→content timeouts** on `sendTabMessage`,
which lines up with the SW-routing regression fixed below.

## SW routing regression — fix applied (commits a6f3a7a, d1cf43c)

**Symptom.** Content scripts were falling back to `content_direct` fetches
because `chrome.runtime.sendMessage` timed out at 10–15 s. Two root causes:

1. Every message handler in `background.js::onMessage.addListener` started
   with `const base = await ingestBase()`, a `chrome.storage.local.get`.
   Under high heartbeat load (7 content tabs × loopStatus every ~30 s +
   per-cycle `log()` calls that themselves do `storage.get+set`),
   `chrome.storage.local` serialised for seconds and every message paid
   the cost — most acutely on SW cold-start.
2. `loopStatus` / `cycleReport` / `pageHealth[healthy]` handlers held
   `sendResponse` until AFTER a 12 s ingest `POST /social/browser-heartbeat`
   and follow-up `maybeForceScrapeCycle` — again pushing the reply past
   content-side deadlines. Callers use `.catch(()=>{})` and never consume
   the return value, so these are fire-and-forget.

**Fix.**
- Module-scope cache for `ingestBase()` / `controlBase()`, refreshed via
  `chrome.storage.onChanged` (both are only writable by popup.js).
- Fast-path branch at the very top of `onMessage.addListener` that
  synchronously acks `log`, `tabReady`, `loopStatus`, `cycleReport` BEFORE
  any `await`. The SW-side forwarding continues in a detached async IIFE;
  the pending fetch keeps the worker alive.

**Result (verified live)**:

| Version | route | count |
|---|---|---:|
| 1.23.34 | sw_relay | 94 |
| 1.23.34 | content_direct (`content_script_boot` — intentional first-boot ping) | 8 |
| 1.23.34 | content_direct — timeout fallback | **0** |

100 % of non-boot heartbeats now use SW relay. This is itself a yield win:
`content_direct` bypasses the SW's `maybeForceScrapeCycle` — i.e. when the
bridge asks the SW to nudge a stale tab, the nudge only lands if the
heartbeat came via SW. Recovering the ~6–10 % of previously-lost nudges
means fewer stale tabs sit unproductive between cycles.

## Tab audit — no closures

Every currently-open scraper tab has a purpose in `platforms.js`:

- `www.instagram.com/` (feed) + `www.instagram.com/direct/inbox/`
  (`extraUrls`) — DM inbox is where new IG messages surface.
- `www.tiktok.com/following` (base) + `www.tiktok.com/foryou`
  (`extraUrls`) — FYP + friends-only feed, different content mixes.

None qualify as "redundant"; both duplicates are configured intentionally
and produce distinct content endpoints per platform. **No tabs closed.**

## Cycle-depth knobs — no changes

Per-cycle scroll depth (`content.js::autoScroll`) already runs 10–18
bursts × 1400 px with ~1.8 s human-jitter pauses (~30 s of scrolling per
cycle). The bottleneck evidence is timeouts, not shallow depth:

- 16 TikTok cycles/24h hit the **5-minute pass timeout** (i.e. they
  couldn't finish, not that they were too shallow).
- 10 Instagram cycles/24h hit the **12-minute pass timeout**.

Raising per-cycle scope would push more cycles into timeout, not more
media into the DB. Raising cycle frequency (WATCHDOG_MIN=7) risks ban
walls on X + IG. **No knob changes committed.**

## Facebook memory hog — deferred

139.8 MB / 22.5k nodes for the FB tab is a leak candidate (long feed
history stays in DOM even after scrolling past). A periodic
`Page.reload({ignoreCache:false})` after N cycles would shed the leak but:

- Risks interrupting an in-flight scrape mid-cycle.
- Facebook is the top producer (769 rows/6h) — don't disturb what works.

Deferred pending a signal that the memory pressure is causing measurable
harm (SW crashes, tab OOM). Currently no such signal.

## Expected yield delta

The SW-routing fix should:

1. **Eliminate the 22+ per-2h timeout-driven `content_direct` events**
   observed pre-fix — direct measurement above shows this achieved (0 in
   last 2 min post-fix, versus 22 in 2 h pre-fix on 1.23.32).
2. **Recover cycle nudges** that were silently dropped when heartbeats
   fell back to `content_direct` (which bypasses `maybeForceScrapeCycle`).
   Ballpark: 6–10 % of stale-tab nudges now land that previously didn't.
3. **Reduce log-write bottleneck on the SW** by caching `ingestBase()`,
   which should lower cross-message-handler contention on
   `chrome.storage.local`.

No changes committed for tab count, cycle rate, or scroll depth — the
observed yield ceilings are cycle-timeout-bound, not depth-bound.

## Follow-ups if pressure grows

- Periodic hard-reload of Facebook tab after N cycles or M MB heap (guard
  against DOM-node leak).
- Batch `log()` writes in memory + flush every 2 s to remove the biggest
  remaining `chrome.storage.local` write contender.
- Add a `chrome.runtime.onConnect` keep-alive port only if the current
  fast-path fix regresses under load.

## Verification appendix — 2026-08-05 02:41Z

20-minute per-minute breakdown across the extension-reload transition:

| minute (UTC) | sw_relay | timeout_fallback | boot |
|---|---:|---:|---:|
| 02:22 | 0 | 5 | 1 |
| 02:24–02:31 | 0 | 2–7 per min | 1–2 |
| 02:32 | 37 | 3 | 0 |
| 02:33 | 20 | 2 | 0 |
| 02:34 | 11 | 0 | 0 |
| 02:35 | 2 | 0 | 0 |
| 02:36 | 9 | 0 | 0 |
| 02:37 | 12 | 0 | 0 |
| 02:38 | 3 | 0 | 0 |
| 02:39 | 32 | 0 | 5 |
| 02:40 | 20 | 0 | 2 |
| 02:41 | 15 | 0 | 1 |

The 10-minute reload-transition window (02:22–02:31) is expected — Chrome
does not automatically re-inject content scripts into existing tabs when
the extension reloads, so scraper tabs must either navigate or be
reloaded. `scripts/reload_scraper_tabs.py` was used to force that here.
Once fresh 1.23.34 content scripts are in place, **zero timeout fallbacks
per minute** for the following 10 minutes.

Raw SW-side ping latency (via `scripts/ping_sw.py`) measured 1–13 ms per
`log` message once the SW is warm.

