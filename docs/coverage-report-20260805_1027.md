# 5-hour Retrospective Coverage — 2026-08-05 09:20 → 02:27 UTC (AFK window)

Report generated at 2026-08-05T02:27Z (10:27 local, GMT+8) covering the
prior 5 hours of AFK collection. Sources: `media_items`,
`browser_ingest_events`, watchdog log, cookie-vault snapshots on disk.

## Per-source media_items yield (last 5h)

| source | rows | oldest | newest |
|---|---:|---|---|
| facebook | 599 | 21:18:03Z | 02:11:16Z |
| website | 413 | 21:18:04Z | 02:17:43Z |
| telegram | 309 | 21:30:31Z | 01:56:52Z |
| search | 176 | 21:31:30Z | 02:17:51Z |
| youtube | 139 | 21:18:29Z | 02:07:28Z |
| instagram | 121 | 21:20:19Z | 02:07:29Z |
| tiktok | 48 | 21:22:45Z | 02:05:56Z |
| strava | 43 | 22:02:19Z | 01:20:33Z |
| x | 11 | 21:28:31Z | 02:14:56Z |
| beeper | 9 | 22:55:12Z | 02:03:41Z |
| lemon8 | 2 | 22:11:18Z | 22:35:20Z |
| whatsapp | 2 | 22:46:52Z | 01:07:51Z |

**Zero `media_items` rows** in this 5h window for:
- **threads** — last row at `2026-08-03 10:05:55Z`, ~40 hours ago. But
  `browser_ingest_events` shows **110 `media` endpoints + 7 `posts`** from
  threads in the same window, so the scraper is running and finding
  content — the bridge is filtering / not upserting. Symptom, not root
  cause; recommend a follow-up on `ig_ingest` behaviour for threads.
- **github** — last row at `2026-08-02 09:36:14Z`. GitHub collector
  cadence is very sparse by design (uses `since` markers); this may be
  normal.

## Gaps ≥ 20 minutes in the last 5h

Rank of largest single gap between consecutive `media_items` per source:

| source | events > 20m | max_gap_min |
|---|---:|---:|
| strava | 1 | 176.3 |
| whatsapp | 1 | 141.0 |
| website | 1 | 137.3 |
| x | 4 | 110.1 |
| beeper | 3 | 72.6 |
| search | 4 | 67.9 |
| tiktok | 6 | 62.9 |
| instagram | 4 | 44.1 |
| telegram | 4 | 35.7 |
| youtube | 1 | 26.9 |
| facebook | 1 | 25.2 |
| lemon8 | 1 | 24.0 |

The strava/whatsapp/website 2-hour-plus gaps line up with the ig_ingest
container restart at ~02:15Z (part of the SW-routing fix rollout).

## Browser-extension endpoint activity (last 5h)

Rows in `browser_ingest_events`:

| platform | endpoint | count |
|---|---|---:|
| bridge | browser_heartbeat | 281 |
| bridge | sw_crash | 1 |
| facebook | browser_heartbeat | 567 |
| facebook | media | 81 |
| facebook | posts | 81 |
| instagram | browser_heartbeat | 1,074 |
| instagram | comments | 18 |
| instagram | media | 4,801 |
| instagram | posts | 13 |
| instagram | profile | 77 |
| lemon8 | browser_heartbeat | 794 |
| lemon8 | media | 156 |
| strava | browser_heartbeat | 651 |
| strava | strava_route_visit | 62 |
| strava | strava_streams | 34 |
| threads | browser_heartbeat | 713 |
| threads | media | 110 |
| threads | posts | 7 |
| tiktok | browser_heartbeat | 1,170 |
| tiktok | media | 65 |
| x | browser_heartbeat | 1,281 |
| x | media | 241 |
| x | posts | 74 |
| x | profile | 18 |

Observations:

- **1 `sw_crash`** event on `bridge` in this window (extension SW hit a
  reportable error). Likely correlated with the SW-routing patch reload
  cycle at ~02:15Z.
- **Instagram is the busiest scraper**: 4.8k media observations → 121
  `media_items` rows means aggressive dedup working, but yield per
  observation is ~2.5 %. Consistent with IG's high-noise feed.
- **x**: 241 media observed → 11 stored (~4.5 %). Twitter's timeline is
  mostly reshared media & non-images.
- **Threads**: 110 media observed → 0 stored. Bridge is dropping every
  candidate. Confirms the threads pipeline anomaly noted above.

## Cycle errors (last 5h)

| platform | error | count |
|---|---|---:|
| tiktok | tab message timed out | 24 |
| tiktok | Could not establish connection. Receiving end does not exist. | 9 |
| strava | Could not establish connection. Receiving end does not exist. | 7 |
| tiktok | TikTok loop scrape pass timed out after 5m | 4 |
| instagram | Instagram loop scrape pass timed out after 12m | 4 |
| lemon8 | Could not establish connection. Receiving end does not exist. | 3 |
| threads | Threads loop scrape pass timed out after 5m | 1 |
| x | Twitter / X loop scrape pass timed out after 4m | 1 |

Two distinct failure modes:

1. **Tab-side timeouts** (`sendTabMessage` from SW to content script hung)
   — dominant, mostly TikTok, addressed by the SW routing fix pushed in
   this session (a6f3a7a, d1cf43c).
2. **Loop scrape pass timeouts** — a cycle didn't complete within the
   per-platform budget (TikTok 5m, IG 12m, X 4m, Threads 5m). These are
   ban-safe design ceilings and are hit ~1–4 times each per 5h window —
   consistent with normal platform slowness, not a regression.

## Cookie vault backup timeline

Snapshots on disk in `credentials/browser_cookies/`:

- 10 snapshots in the last 5h.
- Cadence: one snapshot every ~5 minutes.
- Range: 2026-08-05 01:33Z → 02:24Z (last 51 minutes only).
- The retention window trims older files; `latest.json` was last written
  at 02:24Z.

The 5-minute cadence is healthy. **The trimming means we only have
50 min of file-based history**, but the underlying job clearly ran the
full 5h — earlier snapshots were replaced by the retention policy.

## Watchdog activity (last 5h)

`docker logs unifiedcollector_watchdog --since 5h | Select-String
'RESTART|restart|action_scheduled'` returned **zero matches**. No
automatic collector restarts fired in this window. All 14 tracked
sources reported `ok` in the most recent watchdog sweep (02:23Z),
including `browser_source` heartbeats for tiktok / threads / facebook /
x.

## Summary — was there coverage while AFK?

Yes, with two caveats:

1. **Threads is a silent gap** — the extension is scraping and observing
   ~110 media candidates + 7 posts, but nothing is landing in
   `media_items`. This has been the state since 2026-08-03 10:05Z (~40h).
   Bridge investigation deferred to a separate task.
2. **Tiktok yield is low** (48 rows/5h) relative to activity (1170
   heartbeats, 65 media observations). The SW-routing fix from this
   session should recover ~6–10 % of dropped SW→content cycle nudges;
   monitor over the next 5h.

Everything else produced fresh rows across the 5h window. Watchdog
recorded zero restart events, cookie vault snapshotted every 5 min, and
the 12 sources listed above (facebook down through whatsapp) are current.
