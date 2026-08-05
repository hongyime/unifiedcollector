# Optimization Round 2 — 2026-08-05 (post-retrospective)

Follow-up to `docs/coverage-report-20260805_1027.md`. This round targets
the concrete yield gaps that retrospective flagged, plus a Chrome tab-
group ergonomics fix and a threads status check with the user's new
account.

## Baseline (from the 5h retrospective)

| source    | rows/5h | analyst note in retro                       |
|-----------|--------:|---------------------------------------------|
| facebook  |     599 | top producer, no action                     |
| website   |     413 | fine                                        |
| telegram  |     309 | fine                                        |
| search    |     176 | fine                                        |
| youtube   |     139 | fine                                        |
| instagram |     121 | 4.8k media→121 stored = 2.5% (working)      |
| tiktok    |      48 | 1170 hb + 65 media obs → 48 rows            |
| strava    |      43 | fine                                        |
| x         |      11 | 316 x_posts / 5h, low media because text-heavy |
| beeper    |       9 | 342 beeper_shadow_messages / 5h — display artifact |
| lemon8    |       2 | 156 media obs, only 1 candidate/cycle       |
| whatsapp  |       2 | 83 whatsapp_messages / 5h — display artifact |
| threads   |       0 | 110 media / 7 posts obs → 0 stored (bridge) |

## Investigation findings

### 1. X — HEALTHY, no action needed

`x_posts` has **316 rows / 5h** (query: `SELECT COUNT(*) FROM x_posts WHERE
collected_at > now() - interval '5 hours'`). The low `media_items`
count (11 / 5h) reflects X being predominantly text tweets, not a scraping
gap. `browser_media_candidates` for X shows 10 stored / 20 tiny_thumbnails /
17 duplicates — the pipeline is fine, X just doesn't yield many rich images.

No X selector changes made.

### 2. Lemon8 — ROOT CAUSED + FIXED

The retrospective's "2 rows / 5h, 156 media obs" symptom decomposed like this:

- `browser_media_candidates` had exactly **1 lemon8 row / 5h** with outcome
  `exception`, and `browser_ingest_events` for `platform='lemon8'` showed
  every media endpoint had **`observed_count = 1`** with `reject_stats:
  {"duplicate_sha256": 1}` on every single event.
- Live inspection of the running tab (`scripts/debug_lemon8.py`) confirmed
  DOM state:
  ```
  url: /feed/food?region=sg
  body_first: "Not found ..."
  next_data_present: false
  dom_img_matches: 1  (a static site asset)
  articles/cards: 0
  ```
- Testing 20+ URL variants (`scripts/test_lemon8_urls.py`) showed lemon8's
  SPA now returns "Not found" for every legacy path (`/feed/food`,
  `/foryou`, `/discover`, `/category/food`, `/tag/food`), redirects
  single-segment paths like `/foryou` to `/@foryou` username handles
  (also "Not found"), and only serves real content at
  `/topic/<slug>?region=<cc>`, which resolves to
  `/topic/<numeric_id>?region=<cc>` with 200-300 real post cards.

**Fixes applied**:

- **db30817** — detection: content.js `RECOVERABLE_PAGE_SHELL_PATTERNS.lemon8`
  gains a `not_found` regex under `lowContent`; background.js
  `HOME_NAV_HARD_REFRESH_PLATFORMS` gains `"lemon8"`; `runPageRecovery`
  prefers `hardRefreshNavigationUrl` over `chrome.tabs.reload` when the
  platform is in that set (a reload of a 200-OK-but-broken SPA re-serves
  the same "Not found" page).
- **c313fad** — platforms.js: `lemon8.url` moves from `/` (splash page)
  to `/topic/food?region=sg` (verified 200 with 286 real post cards).
- **b8d6219** — normalization: `shouldNormalizeSingleFeedTab` is broadened
  from just `p.id === "x"` to a `NORMALIZE_PLATFORMS = new Set(["x", "lemon8"])`
  so a lemon8 tab that wandered onto `/` or `/@handle` snaps back to the
  working feed URL on the next watchdog cycle.
- **c694866** *(parallel-subagent commit)* — duplicated the c313fad URL
  fix into background.js's SW-inline registry (verified live via
  `scripts/test_normalize_call.py` that the SW was still reading the
  splash-page URL after c313fad because background.js keeps its own
  inline `UC_PLATFORMS` shadow copy for MV3 boot-safety).

**Observed yield delta (live measurement, ~20 min after fix landed)**:

```
SELECT platform, ingest_mode, outcome, COUNT(*), MAX(last_seen)
FROM browser_media_candidates
WHERE platform='lemon8' AND last_seen > now() - interval '5 minutes'
```

```
 outcome         | count
-----------------+------
 stored          |    28
 http_error      |     3
 duplicate       |     2
 tiny_thumbnail  |     1
```

**28 stored candidates in 5 minutes** vs the prior 2 rows / 5h baseline —
approximately a **170× uplift**. Extrapolated to a 5h window at
current rate → ~1,700 rows / 5h (though this will decay as the topic
feed's initial buffer gets deduped over subsequent cycles).

### 3. WhatsApp — DISPLAY ARTIFACT confirmed

`whatsapp_messages` has **83 rows / 5h** (`SELECT COUNT(*) FROM
whatsapp_messages WHERE collected_at > now() - interval '5 hours'`).
WhatsApp is a text-dominant protocol; the 2 `media_items` rows reflect
only image/video/audio attachments, which are rare per-message. The
underlying platform is healthy. No fix warranted.

### 4. Beeper — DISPLAY ARTIFACT confirmed

`beeper_shadow_messages` has **342 rows / 5h** (`SELECT COUNT(*) FROM
beeper_shadow_messages WHERE ingested_at > now() - interval '5 hours'`).
Same class as WhatsApp — dominant text, few media attachments. Nothing
to fix.

### 5. TikTok — CONTENT-SCRIPT MESSAGING BOTTLENECK, no code change

TikTok's 5h yield window has continued rolling forward to `~9 rows` at
sample time. `tiktok_browser_media_candidates` in the last 5h shows only
3 stored + 3 tiny + 3 duplicate = 8 candidates total; no cycle_error
records post 02:30Z. This is content-side (Chrome background-tab
throttling of content-script cycles), not extension routing — the SW
routing fix already deployed.

Raising per-cycle scroll depth would just push more cycles into the 5m
pass timeout without more content per cycle. Raising WATCHDOG_MIN would
risk ban walls. Deferred.

### 6. Cycle interval (WATCHDOG_MIN=7) — no change

Cycle_error patterns in the last 5h (SELECT platform, err, COUNT):

- 35 IG "tab message timed out"
- 27 tiktok, 26 fb, 12 lemon8, 10 threads — same pattern
- Loop pass timeouts (per platform ceilings hit): IG 3, tiktok 4, threads 1,
  x 6, lemon8 2 = 16 total. **These are the 5-12 min hard caps being reached,
  meaning cycles were already running long. Raising cycle frequency would
  compress recovery time.** No change.

## Task 1 — Chrome tab-group awareness (cc6c039)

`chrome.tabs.query` returns `groupId` on every tab under the base `"tabs"`
permission — the `"tabGroups"` permission is only needed to _create_ or
_modify_ group properties (colour, title, collapse). Verified empirically
against Chrome 150 via `scripts/probe_tabs_group.py`:

```
chrome.tabs.group({tabIds: [tabId], groupId: existing_gid})
-> result: 579543874  (success, no permission error)
```

No manifest change required.

**Implementation**: `background.js` gains two helpers —
`scraperGroupHint()` votes by (groupId, windowId) across existing scraper
tabs to find the user's group, and `createTabInSocialGroup(opts, hint)`
wraps `chrome.tabs.create` with a post-create `chrome.tabs.group`. All
three `chrome.tabs.create` call sites (2 in `ensureScraperTabsOpen`, 1
in `openOrFocus`) now route through this helper. `chrome.tabs.update`
sites need no change because Chrome preserves group membership across
same-window navigations.

**Live verification** (`scripts/verify_tab_group_join.py`): a fresh test
tab (`https://www.facebook.com/help/`) opened via
`createTabInSocialGroup` landed in `groupId: 579543874`, matching all
9 existing scraper tabs. Test tab cleaned up.

## Task 3 — Threads status with the new account

**Login state**: HEALTHY. `Storage.getCookies` (browser-scope) shows for
`.threads.com`:

- `sessionid=63260788288%...` (present)
- `ds_user_id=63260788288` (different from the historically-blocked account)
- `csrftoken` (present, expires 2027)
- `ig_did` (present)
- `mid`, `ps_l`, `ps_n`, `dpr` (present)

The user's new account IS logged in and NOT the 4630001-blocked one.

**Scraper state**: STILL DEGRADED, root cause different from before. Query:

```
SELECT metadata->>'health_status' AS status, COUNT(*)
FROM browser_ingest_events
WHERE platform='threads' AND created_at > now() - interval '15 minutes'
GROUP BY 1 ORDER BY 2 DESC
```

...returns no `recoverable_error_shell` events (so the old
"Meta 4630001 block" pattern is GONE — the page renders normally),
but a stream of:

- `content_script_injected_refresh` (5+ / min)
- `content_script_programmatic_nudge_failed` / `_timed_out`
- `ensure_loop_receiver_missing`
- `forced_cycle_request_timed_out`
- `forced_cycle_reload_skipped` (reload_debounce)

with a matching hard-refresh warning in the SW log:
`Threads: stale forced scrape did not finish after 362s; hard-refreshed tab`.

Content endpoints in the last 15 min: **0 media, 0 posts**. Latest
`threads_posts` row: `2026-08-05 02:06:20Z` (3h+ ago). Latest
`media_items` where source='threads': `2026-08-03 10:05:55Z` (43h+ ago).

**Diagnosis**: content.js *loads* on the threads tab (5+
`content_script_boot` events / 5 min) but the SW's follow-up
`chrome.tabs.sendMessage` calls time out with "receiver missing".
CDP-level `Runtime.evaluate` on the threads tab also times out (>45s
budget), suggesting the tab's main thread is blocked or Chrome's
background-tab throttle has frozen JS execution.

This is a different failure mode from the retrospective's original
"110 media obs, 0 stored" (which was bridge-side filtering, and IS the
parallel subagent's threads-bridge task). What we see now is
extension-side sendMessage timeouts, which is content-side / Chrome
lifecycle — not something this session can fix without either:

- A page reload while the tab is focused (Chrome doesn't throttle
  focused tabs) — user action, deferred.
- Investigation of why content.js's `onMessage.addListener` isn't
  receiving despite the SW logging `content_script_boot`. That warrants
  its own investigation window (likely a race between programmatic
  re-injection and pushState routing on threads' SPA).

**Follow-up recommended**: Bring the threads tab into focus manually
once (the CDP-managed reload from `scripts/reload_threads_tab.py` did
not clear it). If the block clears with a foreground reload, the fix
is one of: (a) periodically bring threads to foreground via
`chrome.windows.update({focused})`, or (b) squelch programmatic
re-injection when the content script has just booted.

## Commit list (this round)

- **cc6c039** feat(extension): add newly-opened scraper tabs to user's social tab group
- **db30817** fix(extension): recover lemon8 tabs stuck on SPA 404 'Not found' page
- **7f280ee** chore(compose): bump UC_EXTENSION_EXPECTED_VERSION to 1.23.36
- **c313fad** fix(extension): update lemon8 canonical URL to /topic/food?region=sg
- **c083d11** chore(compose): bump UC_EXTENSION_EXPECTED_VERSION to 1.23.37
- **b8d6219** fix(extension): normalize lemon8 tabs off canonical path back to platform.url
- **01cb09c** chore(compose): bump UC_EXTENSION_EXPECTED_VERSION to 1.23.38
- **efc8f3c** chore(compose): bump UC_EXTENSION_EXPECTED_VERSION to 1.23.39

(Extension version now 1.23.39 after the pre-commit hook auto-bump
chain. c694866 was a parallel-subagent commit that separately updated
the SW-inline `UC_PLATFORMS` registry copy in background.js.)

## Diagnostic tooling shipped

Retained under `scripts/`:

- `probe_tabs_group.py` — verify `chrome.tabs.group()` permission
- `inspect_tab_groups.py` — enumerate tab-group state via CDP
- `verify_tab_group_join.py` — end-to-end test of tab-group join
- `test_lemon8_urls.py` — batch URL health probe
- `test_lemon8_hydrate.py` — SPA hydration timing
- `debug_lemon8.py` / `peek_lemon8.py` — live DOM inspection
- `test_normalize_call.py` — SW-side normalization dry-run
- `hard_reload_ext.py` — MV3 SW reload via `chrome.runtime.reload()` from tabs.html
- `force_reload_ext3.py` — SW wake + reload with browser-CDP fallback
- `force_normalize_tabs.py` — force `ensureScraperTabsOpen("manual_extension_reload")`
- `sw_log_tail.py` — read `ucLog` from SW storage
- `ping_sw_from_tabs.py` — verify SW responsiveness via `chrome.runtime.sendMessage`
- `check_threads_cookies.py` — read threads.com cookies without touching the tab
- `peek_threads.py` / `peek_threads_via_browser.py` — DOM state of threads tab
- `reload_threads_tab.py` — force `Page.reload` on threads tab

## Deferred / not done

- **TikTok yield lift** — the low candidate count (~8 in 5h) is
  content-side, not scraper-side. A per-tab foreground rotation is the
  most likely lift but needs its own design.
- **Threads bridge-filter fix** — owned by the parallel subagent per
  the retrospective's note. This session confirmed the login state and
  characterized the second-layer content-script sendMessage timeout.
- **Facebook DOM-node leak (139 MB heap, 22.5k nodes)** — deferred in
  the prior scrape-optimization.md, unchanged here. Facebook is still
  the top producer (601 rows / 5h).
