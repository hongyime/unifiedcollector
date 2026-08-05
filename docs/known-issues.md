# Known issues & deferred fixes

Live known-issues log for operators. Every entry cites reproduction steps,
proximal cause, and either the fix that landed or the reason it's deferred.

## 0. Lemon8 SPA URL migration — FIXED 2026-08-05 (Round 2)

**Symptom** — retrospective flagged lemon8 at 2 rows / 5h in `media_items`
despite 156 media obs / 5h in `browser_ingest_events`.

**Root cause** — lemon8's client-side SPA now returns "Not found" for the
prior canonical `/feed/food?region=sg` path (and for every other legacy
path we tried: `/foryou`, `/discover`, `/category/food`, etc.). Every
scrape cycle was collecting exactly one site-default asset image, all
sharing the same sha256 → 100% duplicate rejection.

**Fix** — commits db30817 + c313fad + b8d6219 + c694866:

- Add "Not found" pattern to `RECOVERABLE_PAGE_SHELL_PATTERNS.lemon8`
- Add `"lemon8"` to `HOME_NAV_HARD_REFRESH_PLATFORMS`
- Make `runPageRecovery` prefer URL-navigate over reload for those platforms
- Update `lemon8.url` from `/` to `/topic/food?region=sg` (200-cards/page)
- Broaden `shouldNormalizeSingleFeedTab` beyond just `x`
- Duplicate the URL fix into the SW-inline registry in `background.js`

**Observed uplift**: 28 stored candidates in 5 min post-fix vs 2 rows /
5h pre-fix (~170× rate).

See `docs/optimization-round2.md` for the full investigation.

## 1. "Threads bridge filtering anomaly" — misdiagnosis (2026-08-05)

**Retrospective claim** (`docs/coverage-report-20260805_1027.md`): "110 media +
7 posts observed in `browser_ingest_events` for threads, ZERO stored in
`media_items`." Interpreted as a bridge-side filter dropping all threads
inserts.

**Actual root cause** — not a bridge bug. Verified 2026-08-05:

- Of 96 rows on `endpoint='media'`, 100% are diagnostic **shell probes**
  with `observed_count=0` (`recoverable_error_shell`,
  `no_dom_media_candidates`). These correctly do NOT produce media_items.
- 7 rows on `endpoint='posts'` carry ~26 real posts; those DO land in the
  `threads_posts` table (7074 rows, latest 2 min ago at time of check).
  They intentionally never write to `media_items` — `_save_posts` in
  `src/bridges/ig_ingest.py` writes to per-platform posts tables only.
- Threads media_items rows (kind='post') come from the extension's DOM
  harvest of `img[cdninstagram.net|fbcdn.net]` URLs, downloaded and stored
  by `_drain` / `_download_and_save`. This path has been idle since
  `2026-08-03 10:05:55` because the threads.com page keeps rendering into
  a `something_went_wrong` shell (~171 hits in a 5h window), so the DOM
  never has media candidates to harvest.

**Update 2026-08-05 (Round 2 verification)**: User logged in with a
different account (bypassing the historically-blocked Meta 4630001
account). `Storage.getCookies` for `.threads.com` shows the new
`ds_user_id=63260788288` with fresh sessionid + csrftoken. The
"something_went_wrong" shell IS now gone (no `recoverable_error_shell`
events in the last 15 min). BUT threads-specific content endpoints
(`media`, `posts`) are still at zero because content.js on the threads
tab loads (5+ `content_script_boot` events / 5 min) yet the SW's
subsequent `chrome.tabs.sendMessage` calls time out with "receiver
missing". CDP-level `Runtime.evaluate` on the tab also times out
(>45s), suggesting Chrome has throttled the background tab's JS thread.
Bring threads to foreground once to break out of the throttle; further
fix (auto-foreground rotation or programmatic-inject debouncing) is a
separate investigation.

**What's actually broken and how to spot it**:
- Threads DOM is stuck showing "something went wrong" for the extension
  tab. Not fixable from server side.
- Recovery: open threads.com in the Chrome tab, click "Try again" or
  reload manually. Session cookies are valid (9 threads.com cookies in
  the vault backup, refreshed every 5 min).
- Long-term fix path (deferred, needs Chrome interaction):
  1. Verify session is actually logged in (check `sessionid` cookie).
  2. If broken, log out + re-login. If the shell error persists on a
     freshly logged-in threads.com, it's a Threads-side issue and we
     should widen the recovery-nav rotation.

## 2. Facebook DOM-node leak (~139 MB, 22.5k nodes) — FIXED 2026-08-05

Prior agent measured the FB tab retaining ~22.5k DOM nodes / 139 MB heap
after 4h. A re-measurement at fix time via CDP showed the same tab at
242.7 MB `Performance.getMetrics.JSHeapUsedSize` / 260.4 MB
`performance.memory.usedJSHeapSize` / 18.3k DOM elements / 32.5k Blink
nodes / 27.6k LayoutObjects, so the growth was accelerating past what
the 45-min `ALARM_REFRESH` cycle could contain.

**Fix** — extension 1.23.41+: added `ALARM_MEMORY` (30-min cadence)
that polls memory-sensitive scraper tabs (`facebook`, `threads`) for JS
heap size + DOM node count via `chrome.scripting.executeScript`.
Soft-reloads the tab when any of:

- `performance.memory.usedJSHeapSize` ≥ 250 MB (override via
  `chrome.storage.local.set({memoryReloadThresholdMB: N})`)
- `document.querySelectorAll('*').length` ≥ 40 000
- Time-cap: last memory-driven reload > 4h ago (only after a baseline is
  established, so we don't reload on the very first check post-install)

Never reloads the same tab more often than 90 min. Uses
`chrome.tabs.reload(tabId, { bypassCache: false })` which preserves tab
group + pinned state (same primitive `refreshScraperTabs` uses). Shares
the `lastForcedReloadByTab` debounce map with the forced-cycle recovery
path so we never double-reload. Post-reload, the existing
`chrome.tabs.onUpdated -> kick -> ensureLoops` chain respawns the
content script, and `schedulePostReloadScrapeNudge` re-issues a
`scrapeCycle` message after the platform-specific warm-up delay
(facebook = 30 s).

Observability: emits `browser_heartbeat` events with
`health_status='memory_soft_reload'` /
`'memory_reload_debounced'` / `'memory_check_skipped'` /
`'memory_reload_failed'`, carrying `memory_js_heap_mb`,
`memory_dom_nodes`, `memory_threshold_mb`, `memory_last_reload_age_ms`
in the metadata. `ucStatus.memoryReloadCount` tracks lifetime reload
count for the popup.

## 3. Delta-status cursor cold-start — verified as expected

`notify_status_delta` in `src/notifications/status_delta.py` reads a cursor
from Redis. On cold start with a pre-warmed cursor (from the audit
verification step) the first tick emits no delta. Behaviour is intentional:
better to omit one tick than to spam an empty digest.

## 4. Backup container previously stopped (2026-08-05 recovery)

`unifiedcollector_backup` exited 137 on 2026-07-31 after a pg_dump on
`github_commits` failed with server connection reset. `unless-stopped`
policy doesn't restart on SIGKILL, so it remained down for 4 days.
Restarted at audit time; producing a fresh dump. Watchdog does not
currently monitor the backup service — deferred (add
`unifiedcollector_backup` to the container-liveness sweep).

## 5. `unifiedcollector_backup` next-run gate — informational

The backup loop sleeps 86400s on success, 3600s on failure. After the
2026-07-31 SIGKILL it never re-armed; audit brought it back manually.
