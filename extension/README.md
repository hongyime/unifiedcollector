# UnifiedCollector IG Bridge (Chrome MV3 extension)

Scrapes Instagram media using **your logged-in Chrome session** and forwards it
to the local collector. Because the content script runs *on* instagram.com, its
API calls are **same-origin** and carry your real session cookies — so it
bypasses the login wall / GraphQL-400 rate-limiting the headless `instagram`
collector hits, with a much lower ban profile (it looks like genuine browsing).

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

## Setup

1. **Run the ingest server** (collector side):
   ```
   python -m src.bridges.ig_ingest        # listens on 0.0.0.0:8765
   ```
   (or add it as a docker service; it reads the same DB + `COLLECTOR_DRIVE_PATH`.)
   - `GET /ig/targets` → instagram `collection_targets`
   - `POST /ig/ingest` → downloads each media item + upserts `media_items`

2. **Load the extension**: `chrome://extensions` → enable **Developer mode** →
   **Load unpacked** → select this `extension/` folder.

3. **Configure** (extension popup): set the ingest endpoint
   (`http://127.0.0.1:8765`) and the auto-cycle interval. Keep an
   **instagram.com tab open/pinned** (the content script needs a live IG tab).

4. Click **Scrape now**, or let the alarm run every N minutes. Targets come from
   your collector's `collection_targets` (source=instagram); scraped media lands
   in `media_items` + the media drive, same as every other source.

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

## Files
- `manifest.json` — MV3 manifest (host_permissions for instagram.com + localhost)
- `content.js` — scrapes a profile's media via IG's session API
- `background.js` — service worker: alarms + relays to the ingest endpoint
- `popup.html` / `popup.js` — config + "Scrape now"
- `../src/bridges/ig_ingest.py` — collector-side ingest server
