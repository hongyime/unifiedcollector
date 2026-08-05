# TikTok yield investigation (2026-08-05)

## Symptom

TikTok yielded only 8 candidates in a 5-hour window while facebook
yielded 599 in the same time. Prior agent labeled this "content-side
throttling" and deferred.

## Root cause analysis (24h before fix)

Cycle count itself is healthy:

| platform  | heartbeats/24h | media events/24h | observed | stored | store % |
|-----------|----------------|------------------|----------|--------|---------|
| tiktok    | 4059           | 232              | 2870     | 119    | **4.1%** |
| facebook  | 2263           | 352 (posts) + 332 (media) | 13344 | 3155 | 23.6% |
| instagram | 4242           | 13881 (media)    | 27007    | 7824   | 28.9% |
| threads   | 3144           | 345 (media)      | 479      | 141    | 29.4% |

TikTok cycles fire *more* often than facebook. The bottleneck is
per-candidate rejection, not scrape frequency.

### Rejection breakdown (last 24h, tiktok_browser_media_candidates)

| outcome          | reason                                                | count |
|------------------|-------------------------------------------------------|-------|
| duplicate        | duplicate_sha256                                      | 185   |
| duplicate        | duplicate_content_id                                  | 80    |
| stored           | (success)                                             | 51    |
| exception        | exception                                             | 34    |
| tiny_thumbnail   | too small (~15KB < 20KB image min)                    | 74    |
| http_error       | 403                                                   | 11    |
| disallowed_host  | v16-webapp-prime.tiktok.com not on allowlist          | 10    |
| deferred         | deferred_upload_budget (tiktok limit = 1/cycle)       | 7     |

### Two independent root causes

1. **Feed diversity is too low.** 51% of what passes the tiny_thumbnail
   filter is a `duplicate_sha256` or `duplicate_content_id` — the same
   cover URL / content_id has already been stored. `/foryou` is highly
   personalized and returns the same 20-30 items across visits on a
   stable session; `/following` limits candidates to followed accounts.
   Small pool → high recycle rate.

2. **Videos are effectively unreachable.** Every observed
   `video_playaddr` (7/day) and `dom_video` (3/day) was rejected as
   `disallowed_host`. Root cause: TikTok's video CDN
   `v16-webapp-prime.tiktok.com` is a *.tiktok.com subdomain, not on
   the extension's `browserUploadAllowed()` regex which only allows
   `tiktokcdn|tiktokv|byteoversea|byteimg|ibytedtos|muscdn`. Even for
   ones that would pass, `BROWSER_UPLOAD_ATTEMPT_LIMIT_BY_PLATFORM.tiktok
   = 1` caps at one video upload per cycle. That's why the 24h stored
   set is 100% covers + dom_images, zero actual videos.

## Fix applied (v1.23.42) — URL rotation, MIXED result

Commit `35f0ee7` (this branch) added `https://www.tiktok.com/explore`
to `UC_PLATFORMS.tiktok.extraUrls` in both `extension/platforms.js`
and `extension/background.js`. Rationale: extend the URL rotation
beyond /foryou (personalized, recycle-prone) and /following (small
graph) with a public trending page.

Extension re-loaded via CDP at ~09:35 UTC. Post-fix DOM probe:
* /foryou: `title="(10)"`, 10 imgs, 6 articles, has_state=false — produces candidates.
* /following: (redirect-heavy, alternates between loaded and reloading)
* /explore: **`title="TikTok - Make Your Day"`, imgs=1, articles=0** —
  **not a content feed; it's a category picker.** The body sample is
  category chips ("Singing & Dancing / Comedy / Sports / …"). Navigating
  to `/explore?category_type=N` also gave `imgs=0 articles=0`. Earlier
  in the reload sequence the page briefly showed 120 articles + 7 images,
  but that was transient and settled to the empty picker.

Post-fix 35-min observation (2026-08-05 09:35 → 10:10 UTC):
* tiktok_browser_media_candidates: 2 stored + 1 tiny_thumbnail (all
  from `/foryou`, zero from `/explore` or `/following`)
* media_items rows: 2 (both /foryou-sourced)

Verdict: **the /explore rotation is inert.** It spawns a third scraper
tab, that tab loads TikTok's category picker (not a feed), the scraper
runs one cycle finding no media, and moves on. Doesn't harm baseline
but doesn't help either.

## What actually needs to happen

### Option A — swap /explore for a hashtag or user feed page (extension change)

TikTok's real "browsable public feed" URLs are:

* `/tag/<hashtag>` — hashtag feed page. Has `__UNIVERSAL_DATA_FOR_REHYDRATION__`
  state with media items. Requires picking a hashtag (fragile: needs
  refresh strategy when the tag falls out of trending).
* `/@<username>` — user profile grid. Same state format. Rotation would
  need a list of followed usernames, ideally sourced from
  `tiktok_profiles` where `is_following=true`.

Recommend: build a small revisit-style queue that picks a followed
username per cycle from `tiktok_profiles`, rotates across users, and
feeds it as an extraUrls-like target. This is more surgical than a
static URL list.

### Option B — unblock videos (extension + manifest change, this is the biggest single lever)

Three coordinated changes:

1. `extension/background.js` `browserUploadAllowed()` regex — add
   `tiktok.com` to the tiktok allowlist so `v16-webapp-prime.tiktok.com`
   passes the host check.
2. `extension/manifest.json` `host_permissions` — add
   `https://*.tiktok.com/*` so the MV3 SW is actually allowed to fetch
   from that host (currently only `https://www.tiktok.com/*` is listed,
   which does NOT cover subdomains).
3. `extension/background.js` `BROWSER_UPLOAD_ATTEMPT_LIMIT_BY_PLATFORM.tiktok`
   — bump from `1` to `3` so more than one video per cycle can upload
   before `deferred_upload_budget` kicks in.

Risk: adding `https://*.tiktok.com/*` triggers a Chrome permission
dialog on next extension update. Also video CDN URLs may still 403 due
to hotlink protection independent of the allowlist — needs a first
real-world attempt to confirm. If they do 403, expected impact is
smaller than the diversification fix; if they succeed, this recovers
the "actual video" content that's currently 100% lost.

### Option C — multi-account rotation (design only, per instructions)

The observed 4.1% acceptance rate on 2870 candidates suggests
per-account throttling: the same user session sees the same
personalized feed cache repeatedly, driving duplicate_sha256 to 51%.
Rotating between 2-3 TikTok accounts (mirroring the telegram
multi-account pattern) would multiply the effective candidate pool.

This was called out in the task as design-only. Full design:

* Cookie-vault entries already exist per-account (see
  `browser_cookie_vault`), so accounts can be switched by pushing a
  different cookie set into Chrome via CDP.
* A scheduler alarm rotates the active tiktok cookie set every N
  minutes (e.g., 60m per account so TikTok sees a "consistent session"
  window on each), navigating the pinned tab to `/foryou` after each
  swap.
* Complications: TikTok will notice the sudden identity change and may
  flag it; needs to hold the cookie swap behind a short
  "logged-out-then-relogin" window to avoid tripping session-integrity
  checks. Also profile-linked scraping (username, follow graph) needs
  per-account attribution so cross-account harvest doesn't mash owners.

Recommend building this only after Option A + Option B are validated
and the ceiling is confirmed to be account-side.

## Constants and files touched

* `extension/manifest.json` version 1.23.40 → 1.23.42 (this fix)
  * v1.23.41 belongs to subagent A's memory-check work (commit `352ae86`)
* `docker/docker-compose.yml` UC_EXTENSION_EXPECTED_VERSION x3 → 1.23.42
* `extension/platforms.js` UC_PLATFORMS tiktok extraUrls
* `extension/background.js` UC_PLATFORMS tiktok extraUrls

Commit: `35f0ee7 feat(extension): add tiktok /explore url to break duplicate-feed loop (v1.23.42)`
Tests: `python -m pytest tests/extension/test_extension_bundle_static.py` — 25 passed.
