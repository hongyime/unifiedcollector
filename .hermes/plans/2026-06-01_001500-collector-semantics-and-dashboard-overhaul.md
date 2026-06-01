# Plan: Collector-semantics overhaul + dashboard fixes + verbose error surfacing

**Date:** 2026-06-01
**Repo:** C:\unifiedcollector
**Status:** PLAN ONLY — nothing changed. Read-only investigation done; findings below are verified against live DB + source unless marked OPEN.

---

## Goal

Two tracks:

**Track A — make every collector target the RIGHT things** (your own/owned/personal
graph + spider out from it; AVOID famous accounts/repos), not the placeholder
demo/famous seeds currently in `collection_targets`.

**Track B — fix the dashboard** (verbose raw errors, Strava tab, collectors status,
targets display, media filters, pagination).

---

## Verified current state (the "why")

`collection_targets` today (live query) — confirms your complaints:

| source | count | what's actually there | verdict |
|---|---|---|---|
| telegram | 22 | `CoinDesk`, `durov`, `SpaceX`, `bellingcat`, `TheEconomist`, `python`, `HackerNews`, `OSINTtechniques` + some numeric IDs | WRONG — predefined famous public channels, NOT your connected accounts' dialogs |
| strava | 1 | `me` only | WRONG — no `feed`, no following/follower spider |
| youtube | 4 | `UC-lHJZR3...` (PewDiePie), Kurzgesagt, MKBHD, Fireship | WRONG — famous demo channels, the exact opposite of what you want |
| github | 17 | `torvalds`, `pytorch/pytorch`, `huggingface/transformers`, `ggerganov/llama.cpp`, `sindresorhus`, `tj` | WRONG — famous repos/accounts |
| lemon8 | 60 | personal handles (`shotsbyseah`, `bryanseah`, `prawnproductions`, …) | mostly RIGHT, but no feed-spider confirmed |
| tiktok | 240 | personal handles | looks RIGHT |
| website | 421 | SG school/uni domains (`*.moe.edu.sg`, NUS, SMU, …) | RIGHT (SG schools, as you said) |
| instagram | 9 | personal + famous mix | EXCLUDED per earlier instruction |

**Machinery already exists** (so this is mostly re-targeting + flag-flipping, not greenfield):
- Telegram: `collect_dialogs()` (iter_dialogs across all workers → upsert `telegram_chats`),
  `_spider_enqueue`, forward/reaction seed enqueue all present in
  `src/collectors/telegram/__init__.py`. It's just being fed predefined `@channel`
  targets instead of being told "enumerate the connected accounts' own dialogs."
- Strava: `_collect_feed()`, `collect_following_roster()`, `_process_spider_queue()`,
  `strava_spider_queue` all present in `src/collectors/strava/__init__.py`. Gated behind
  `STRAVA_FOLLOW_SPIDER` env + `_use_web`; `strava.targets` is just `me`.

**service_cursors** (powers Collectors tab): every source row exists with recentish
`last_processed_at` but `status='idle'` between cycles; only `_worker` and `strava`
showed `running`. So "only _worker active" = sources flip to `idle` after each cycle and
the tab has no liveness/last-seen rendering → looks dead.

**Dashboard root causes (verified):**
- `/whatsapp/users` 500: `to_regclass('wa_user_profiles')` = NULL — table doesn't exist
  (WhatsApp not linked yet). Endpoint does bare `SELECT * FROM wa_user_profiles`.
- No global exception handler in `src/dashboard/api.py` → unhandled errors return
  FastAPI's generic `500 Internal Server Error` with no detail. (You want raw errors.)
- Targets tab "only shows source": `/targets` returns `target_id` (SELECT * ✓) but
  `TargetsPage.tsx` line 124 renders `t.target` — **field-name mismatch**, so the target
  column is blank. Pure frontend bug.
- Collectors pagination + media-browser filters: frontend-only.

---

## Track A — collector targeting (per source)

### A1. Telegram — scrape EVERYTHING from connected accounts (not predefined channels)
- **Remove** the 22 predefined public-channel targets from `telegram.targets` /
  `collection_targets`.
- **Seed with `dialogs`** sentinel (or per-account `dialogs:<account>`) so `run_targets`
  routes to `collect_dialogs()` → enumerates every chat/channel/group each of the 4
  connected accounts is actually in, upserts to `telegram_chats`, enqueues each as a
  spider seed for message backfill.
- Confirm the spider tricks you taught: forward-source enqueue (line ~1109), per-user
  reaction-list enqueue (line ~1257), participant enumeration. These already exist —
  ensure they're ENABLED (env flags / not short-circuited).
- Net effect: collection follows YOUR accounts' real graph, spiders outward from it.
- **Files:** `config/sources/telegram.targets`, possibly `src/collectors/telegram/__init__.py`
  (verify the `dialogs` sentinel is honored by `run_targets`), DB cleanup of old targets.

### A2. Strava — foryou feed + following + followers + spider (media/map only), famous-agnostic
- `strava.targets`: add `feed` (and keep `me`). 
- Set `STRAVA_FOLLOW_SPIDER=1` + `STRAVA_USE_WEB=1` (web cookie path) in
  `config/sources/strava.env`.
- Spider rule per your spec: for every athlete in feed/following/followers, ingest
  activities **that have media or map/GPS data**, enqueue their following+followers,
  recurse — `dont care famous or not` (Strava is the ONE source with no famous filter).
- Verify `collect_following_roster()` pulls BOTH following and followers (spec says
  both). If it only does `/follows` (following), add the followers page.
- **Files:** `config/sources/strava.targets`, `config/sources/strava.env`,
  `src/collectors/strava/__init__.py` (confirm followers + media/map filter).

### A3. YouTube — MY subscriptions + interest filter, AVOID famous; seed from old DB
- Replace the 4 famous demo channels.
- **Seed source FOUND:** `archive/youtubetoolkit/data/subscriptions.json` = **492 real
  subscriptions** (channel_id + name + url), e.g. Marcus Hutchins, Jet Lag, Theo t3.gg,
  Tom Scott, Low Level, fern. Import all 492 channel IDs as youtube targets.
  (`youtube_data.db` has 208k videos but empty `channels` table; `target_channels.txt` is
  135 lines mostly comments — the JSON is the seed.)
- **Famous filter (CONFIRMED): YouTube < 4k subscribers.** The JSON has NO sub-count
  field, so the cap must be applied at collection time: fetch each channel's subscriber
  count, skip if >= 4k.
- **DECIDED: filter OVERRIDES the seed (option b).** The <4k cap applies uniformly — even
  to your 492 subs. Subs that are famous (Fireship, Tom Scott, Morning Brew) WILL be
  filtered out. The seed casts a wide net; the famous filter trims it. NO allowlist bypass
  for any source (same rule for GitHub repos and your own social handles).
- Pull live subscriptions via the authed account if a cookie/OAuth is available (keeps the
  seed fresh vs the 2026-05-15 snapshot).
- **Files:** `config/sources/youtube.targets` (import 492), `config/sources/youtube.env`
  (`YOUTUBE_FAMOUS_SUB_CAP=4000`, allowlist flag), `src/collectors/youtube/` (sub-count
  filter + subs pull).

### A4. GitHub — interest filter, AVOID famous
- Replace `torvalds`/`pytorch`/`huggingface`/etc.
- **Famous filter (CONFIRMED): GitHub < 1k stars.** Skip repos above 1000 stars; prefer
  your follows / owned / starred-by-you graph. (Same allowlist tension as YouTube: your
  own/owned repos bypass the cap.)
- **Files:** `config/sources/github.targets`, `config/sources/github.env`
  (`GITHUB_FAMOUS_STAR_CAP=1000`), maybe `src/collectors/github/` for the star cap.

### A5. Lemon8 — foryou feed + profile accounts, AVOID famous; seed from old DB
- Keep the 60 personal handles already in `collection_targets`; add feed-spider if the
  collector supports a `feed` sentinel.
- **Seed source CHECKED:** `archive/lemon8toolkit/src/data/lemon8_toolkit.db` `users`
  table is **EMPTY** — no usable seed in the DB. The 60 personal handles already targeted
  ARE the real seed; no import needed.
- **Famous filter (CONFIRMED): Lemon8 < 1k followers.** Skip profiles above 1000 followers
  for spider-discovered accounts.
- **Files:** `config/sources/lemon8.targets`, `config/sources/lemon8.env`
  (`LEMON8_FAMOUS_FOLLOWER_CAP=1000`), maybe `src/collectors/lemon8/`.

### A6. Search — terms TBD by you; old searchtoolkit had none
- **Seed CHECKED:** `archive/searchtoolkit/state/state.db` `query_progress` table is
  **EMPTY** — no old query list to reuse. You'll supply terms.
- **Files:** `config/sources/search.targets`.

### A7. Website — already correct (SG schools). No change.

### A8. Instagram + other social — famous-filter thresholds (for when re-enabled)
- Instagram is currently EXCLUDED/disabled (no-proxy wait-out). When re-enabled, apply
  the same famous-avoidance. **Proposed Instagram cap: < 1k followers** (matches the
  Lemon8 follower cap; confirm). Your own/owned handles (bryanseah234, shotsbyseah234,
  prawnproductions234) bypass the cap as priority-10 allowlist.
- General rule for any follower-based social (instagram, lemon8, tiktok): spider-discovered
  accounts kept only if < 1k followers; explicitly-seeded/owned handles always kept.
- TikTok: 240 personal handles already targeted; add the < 1k follower cap for
  spider-discovered profiles only.

### Schema/data reset — AUTHORIZED
User authorized deleting wrongly-collected rows and resetting collectors from scratch
where schema was wrong or data was collected against the wrong targets. So: purge the
famous/predefined `collection_targets` (telegram public channels, youtube demo channels,
github famous repos) and any media_items/source rows collected from them, rather than
leaving stale wrong data. Re-seed clean. Back up first (pg_dump) per the backup hardening.

---

## Track B — dashboard fixes

### B1. Verbose raw errors (do this FIRST — it makes everything else diagnosable)
- Add a global exception handler in `src/dashboard/api.py` that, when `_AUTH_DISABLED`
  (localhost), returns the real exception type + message + traceback tail in the JSON
  body (instead of generic 500). Gate the traceback behind the localhost flag so it never
  leaks in a networked deploy.
- Add structured request/error logging (method, path, exception, stack) at WARNING+.
- **Files:** `src/dashboard/api.py`.

### B2. `/whatsapp/users` 500 → graceful empty
- Guard `wa_user_profiles` (and `wa_user_history`) with `to_regclass` existence check;
  return `[]` (and a `meta: {table_missing: true}` hint) when the table isn't created
  yet (pre-link). Same defensive pattern as the telegram_stats `_count` helper.
- **Files:** `src/dashboard/api.py`.

### B3. Targets tab shows the actual target
- Fix `TargetsPage.tsx`: render `t.target_id` (API field) not `t.target`. Add columns:
  target_id, source, status, priority, last_collected. Add a per-source detail so it's
  not just "source = instagram" with no target.
- **Files:** `dashboard/frontend/src/features/targets/TargetsPage.tsx`,
  `dashboard/frontend/src/services/types.ts` (Target type: `target` → `target_id`).

### B4. Collectors tab — show liveness, drop pagination
- Render last-seen / last_processed_at + a "stale?" indicator so a source between cycles
  reads as "alive (idle)" not "dead." Optionally compute running/idle from
  `last_processed_at < N min ago`.
- Remove pagination — all ~11 sources fit one table.
- Decide: should per-source `status` in `service_cursors` reflect "alive" better? (The
  collectors DO update their row each cycle; the flip to idle is expected.) Likely a
  display fix, not a backend one.
- **Files:** `dashboard/frontend/src/features/collectors/CollectorsPage.tsx`
  (+ DashboardPage.tsx for the Strava-missing-from-dashboard issue).

### B5. Strava on Dashboard tab + Strava feed page
- "strava also not showing up on Dashboard tab" — check `DashboardPage.tsx` source list /
  `SOURCES` constant includes strava; check the Strava feed page (`/strava/feed`) renders
  athletes+activities (you said "all I see are athletes" — likely activities query empty
  because feed-spider is off; fixing A2 populates it).
- **Files:** `dashboard/frontend/src/features/collectors/DashboardPage.tsx`,
  `dashboard/frontend/src/features/strava/StravaFeedPage.tsx`, `utils/constants.ts` (SOURCES).

### B6. Media browser proper filter dropdowns
- Add dropdowns: source, media type (image/video/doc), date range, has-media. Wire to
  existing `/media` query params (verify what `MediaBrowserPage` / `/media` supports).
- **Files:** `dashboard/frontend/src/features/media/MediaBrowserPage.tsx`, maybe
  `src/dashboard/api.py` `/media` for missing filter params.

### B7. Console logging verbose for triage
- Frontend: log failed requests (status + URL + response body) to console in dev.
- Backend: per B1, structured error logs.

---

## Step-by-step execution order (when we build)

1. **B1 verbose errors** — unblocks diagnosis of everything else.
2. **B2 whatsapp 500 guard** — stops the noisy 500.
3. **B3 targets display** + **B4 collectors** + **B6 media filters** — frontend batch, one
   `npm build` + bake (Pattern B: rebuild dashboard image).
4. **A2 Strava** (config + env, verify followers/media filter) — restart strava collector.
5. **A1 Telegram** (switch to dialogs spider, purge predefined targets) — restart main.
6. **A4 GitHub** + **A3 YouTube** + **A5 Lemon8** famous-filters — needs seed DBs (OPEN).
7. **A6 Search** — when you send terms.
8. **B5 Strava-on-dashboard** — verify after A2 populates data.

Config edits are file-authoritative (Option A): edit `config/sources/*.{targets,env}`,
then restart the relevant collector. Code edits to collectors/dashboard need image
rebuild (Pattern B — `compose up` reverts docker-cp). env_file changes need `compose up`,
not restart.

## Files likely to change (summary)
- Configs: `config/sources/{telegram,strava,youtube,github,lemon8,search}.{targets,env}`
- Backend: `src/dashboard/api.py` (B1, B2), collectors `src/collectors/{strava,youtube,github,lemon8,telegram}/`
- Frontend: `TargetsPage.tsx`, `CollectorsPage.tsx`, `DashboardPage.tsx`,
  `StravaFeedPage.tsx`, `MediaBrowserPage.tsx`, `services/types.ts`, `utils/constants.ts`
- DB: purge wrong `collection_targets` rows (telegram/youtube/github famous seeds)

## Tests / validation
- `ast.parse` + `ruff E9,F821` on touched .py; `npm run build` (tsc+vite) for frontend.
- Per source after re-target + restart: confirm real production (e.g. telegram pulling
  YOUR dialogs' messages not @coindesk; strava feed activities with media/map appearing;
  youtube/github skipping famous).
- Dashboard: trigger a known-bad endpoint, confirm RAW error JSON now returned; targets
  tab shows target_id; collectors tab one page with liveness; media filters work.
- Empirical observe 1+ cycle per source post-change.

## Risks, tradeoffs, OPEN questions
- **OPEN (blocking A3/A5/A6):** youtubetoolkit / lemon8toolkit / searchtoolkit DB
  locations not found in the searched paths. Need exact paths (or copy them into the
  repo). Until then, those seed-imports can't proceed.
- **"Not famous" filter definition:** need thresholds — YouTube subs cap? GitHub stars
  cap? Lemon8 followers cap? Propose defaults (e.g. youtube < 100k subs, github < 5k
  stars, lemon8 < 50k followers) for you to confirm.
- **Telegram `dialogs` re-target** will massively expand scope (every chat in 4 accounts
  → could be thousands of chats + heavy message backfill). Confirm rate-limit / FloodWait
  handling and a sane backfill depth before unleashing.
- **Strava followers scrape** may need a different endpoint than following; verify the
  web path exists or add it.
- **Traceback in API responses** must stay localhost-only (B1 gated on `_AUTH_DISABLED`) —
  never leak stacks on a networked deploy.
- WhatsApp standalone collection (so linked accounts' chats actually ingest) is a SEPARATE
  larger piece you flagged ("all chats/channels/groups of connected accounts") — not fully
  scoped here; the bridges currently feed beeper_shadow, not a wa collector. Flag for its
  own plan.
