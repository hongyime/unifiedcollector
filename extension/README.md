# UnifiedCollector Bridge (Chrome MV3 extension)

Collects social media using **your logged-in Chrome session** and forwards it to
the local collector. Because the content script runs *on* the social site, its API
calls are **same-origin** and carry your real session cookies + browser
fingerprint — so it bypasses the login wall / rate-limiting that the headless
collectors hit, with a much lower ban profile (it looks like genuine browsing).

**Multi-platform:** scrapers live in a `PLATFORMS` registry in `content.js`.
**Instagram** is implemented; others (TikTok, Twitter/X, …) can be added without
touching the rest — see "Adding a platform" below.

**Observability (popup):** dark-mode popup with a live **Status** panel (worker
alive, social tab open, ingest connected, cycle cadence + next wake, last-cycle
stats) and a scrolling **Activity log** of every cycle/ingest/discover/error.
A branded icon ships in `icons/`.

**Social tab launcher (`tabs.html`):** a dark page (popup → "🗂 Manage social
tabs", or right-click the icon → Options) that lists every platform with live
status — **tab open?** and **logged in?** (detected via each platform's auth
cookie) — plus **Open / Open all** buttons. Open them once, log in, and the
bridge scrapes from those pinned tabs. Platforms come from `platforms.js`
(Instagram = scraper active; TikTok/Lemon8/X/Facebook/Threads = login-ready).

## Architecture

```
[instagram.com tab]  content.js  --(scrape via IG api/v1, session cookies)-->  IG
        |  (chrome.runtime messages)
        v
   background.js (service worker)  --(HTTP)-->  ig_ingest.py (collector)  -->  media drive + media_items
        ^
        |  chrome.alarms every N min triggers a scrape cycle
```

Same consumer pattern as the WhatsApp/Beeper bridges: an external local service
feeds the collector over a local HTTP endpoint.

## Setup (zero-config — install + leave a tab open)

1. **Ingest server**: runs automatically as the `ig_ingest` docker service
   (auto-restart, publishes `127.0.0.1:8765`). It comes up with the stack:
   ```
   docker compose up -d ig_ingest
   ```
   No manual host process. Code edits apply with `docker restart
   unifiedcollector_ig_ingest` (live `../src` mount, no image rebuild).

2. **Load the extension** (once): `chrome://extensions` → **Developer mode** →
   **Load unpacked** → select this `extension/` folder.

3. **Keep one instagram.com tab open/pinned, logged in.** That's it — **no popup
   configuration needed.** Defaults already point at `http://127.0.0.1:8765` and
   auto-run a scrape cycle every 30 min via `chrome.alarms`. (The popup is only
   there if you ever want to change the endpoint/interval or hit "Scrape now".)

Targets come from `collection_targets` (your seeds) plus spider-discovered
profiles; scraped media lands in `media_items` + the media drive like every other
source.

## 2-hop spider (friends-of-friends)

The extension doesn't just scrape your fixed seed list — for any profile at
**hop < 2** it also harvests that profile's followers + following and POSTs them
to `POST /ig/discover`, which stores them at hop+1 in `instagram_spider_targets`
(a channel separate from `collection_targets`, so the `.targets` file-sync never
wipes them). Hop 0 = your seeds, hop 1 = their network, hop 2 = leaf (scrape
only). **Famous accounts (> `INSTA_SPIDER_FAMOUS_CAP`, default 100k followers) are
skipped** — we crawl your network, not celebrities. Tune via env on the
`ig_ingest` service: `INSTA_SPIDER_HOPS`, `INSTA_SPIDER_FAMOUS_CAP`,
`IG_SPIDER_TARGETS_LIMIT` (max targets served per cycle).

## MV3 notes / maintenance

- **Service worker is ephemeral** → scraping is driven from the content script on
  a live IG tab, nudged by `chrome.alarms` (not a persistent background loop).
- **Endpoints drift**: IG rotates internal API params periodically. If
  `web_profile_info` / `feed/user` stop returning data, open DevTools → Network →
  Fetch/XHR while scrolling a profile, copy the current request, and update the
  URLs in `content.js`.
- **Pace it**: there are randomized delays between pages/profiles. Don't crank
  them down — behavioural detection can still action even a real session.
- **One account = one point of failure.** Use a throwaway/secondary IG login.

## Adding a platform
1. Add an object to the `PLATFORMS` registry in `content.js` with a `host`
   matcher, a `label`, and an `async runCycle()` returning `{targets, saved,
   discovered}` (use `clog(level, msg, label)` to surface progress in the popup).
2. Add the site to `content_scripts.matches` + `host_permissions` in
   `manifest.json`, and to `SOCIAL_URLS` in `background.js`.
3. Add the matching collector-side ingest endpoints (mirror `/ig/targets`,
   `/ig/ingest`, `/ig/discover`).

## Files
- `manifest.json` — MV3 manifest (host_permissions for instagram.com + localhost)
- `content.js` — scrapes a profile's media via IG's session API
- `background.js` — service worker: alarms + relays to the ingest endpoint
- `popup.html` / `popup.js` — config + "Scrape now"
- `../src/bridges/ig_ingest.py` — collector-side ingest server
