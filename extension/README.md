# UnifiedCollector Bridge (Chrome MV3 extension)

Collects browser-visible social content using the logged-in browser session and
forwards it to the local collector. Requests run in the site's page context;
source rate limits, login walls and challenges still apply.

**Multi-platform:** the scraper registry is in `extension/src/content.ts`.
Current page scrapers are Instagram, Threads, TikTok, Lemon8, X, Facebook and
Strava. Lemon8's no-login For You tab adds paced browser media candidates to
the headless API collector's profile, feed, and topic collection.

**Observability (popup):** dark-mode popup with a live **Status** panel (worker
alive, social tab open, ingest connected, cycle cadence + next wake, last-cycle
stats) and a scrolling **Activity log** of every cycle/ingest/discover/error.
A branded icon ships in `icons/`.

**Social tab launcher (`tabs.html`):** a dark page (popup → "🗂 Manage social
tabs", or right-click the icon → Options) that lists every platform with live
status — **tab open?** and **logged in?** (detected via each platform's auth
cookie) — plus **Open / Open all** buttons. Open the platforms you want to
collect and sign in where required. Tab/login requirements are listed in
`extension/src/shared/platforms.ts`.

## Architecture

```
[platform tab]  content script  --(page/session capture)-->  browser evidence
        |  (chrome.runtime messages)
        v
   background service worker  --(HTTP)-->  browser_ingest bridge (:8765)
                                      --> Collector database + media vault
```

Same consumer pattern as the WhatsApp/Beeper bridges: an external local service
feeds the collector over a local HTTP endpoint.

## Setup

1. **Ingest server**: runs as the `browser_ingest` service in
   `docker/docker-compose.yml` and listens on port 8765. Start it with:
   ```
   docker compose -f docker/docker-compose.yml up -d browser_ingest
   ```
   The service uses the bind-mounted `src/bridges/ig_ingest/` package.

2. **Load the extension** (once): `chrome://extensions` → **Developer mode** →
   **Load unpacked** → select this `extension/` folder.

3. **Open a tab for each platform you want to collect.** Sign in where the
   platform requires it. The extension uses its configured local endpoint,
   defaulting to `http://127.0.0.1:8765`.

Instagram and other platform targets come from their source queues and
discovery paths. Captured media lands in `media_items` and the shared vault.

## Instagram two-hop spider (friends-of-friends)

The extension doesn't just scrape your fixed seed list — for any profile at
**hop < 2** it also harvests that profile's followers + following and POSTs them
to `POST /ig/discover`, which stores them at hop+1 in `instagram_spider_targets`
(a channel separate from `collection_targets`, so the `.targets` file-sync never
wipes them). Hop 0 = your seeds, hop 1 = their network, hop 2 = leaf (scrape
only). **Famous accounts (> `INSTA_SPIDER_FAMOUS_CAP`, default 100k followers) are
skipped** — we crawl your network, not celebrities. Tune via env on the
`browser_ingest` service: `INSTA_SPIDER_HOPS`, `INSTA_SPIDER_FAMOUS_CAP`,
`IG_SPIDER_TARGETS_LIMIT` (max targets served per cycle).

## MV3 notes / maintenance

- **Service worker is ephemeral** → scraping is driven from the content script on
  a live IG tab, nudged by `chrome.alarms` (not a persistent background loop).
- **Endpoints drift**: Instagram rotates internal API params periodically. If
  `web_profile_info` / `feed/user` stop returning data, open DevTools → Network →
  Fetch/XHR while scrolling a profile, copy the current request, and update the
  URLs in `extension/src/content.ts`.
- **Pace it**: there are randomized delays between pages/profiles. Don't crank
  them down — behavioural detection can still action even a real session.
- **One account = one point of failure.** Use a throwaway/secondary IG login.

## Throttle/backoff knobs

Throttle walls are persisted per platform in the content script so a tab refresh
or service-worker wake does not immediately re-hit a flagged account. Defaults
are intentionally conservative:

```js
{
  instagram: 45, // minutes; keep cautious, this is the known IG throttle wall
  threads: 12,
  x: 12,
  tiktok: 20,
  facebook: 20,
  lemon8: 20,
  default: 30
}
```

Override without editing code from the extension DevTools console:

```js
chrome.storage.local.set({
  ucThrottleBackoffMins: { threads: 10, x: 10, tiktok: 20, instagram: 45 }
});
```

Do not shorten Instagram aggressively. The 45-minute default is deliberate: repeated
early probes extend the IP/account throttle window and raise ban risk.

## Platform registry and files

Platform metadata and login detection live in `src/shared/platforms.ts`; page
capture lives in `src/content.ts`. The extension build emits the scripts loaded
by `manifest.json` under `dist/`.

- `manifest.json` — MV3 manifest and content-script permissions
- `src/content.ts` — platform page capture and scrape cycles
- `src/background.ts` — service worker and local bridge delivery
- `src/shared/` — platform metadata, cooldowns, logging and ingest client
- `src/popup.ts`, `src/tabs.ts` — popup and platform-tab management
- `../src/bridges/ig_ingest/` — collector-side aiohttp ingest package

<!-- extension version is auto-bumped by scripts/hooks/pre-commit on any extension change. -->
