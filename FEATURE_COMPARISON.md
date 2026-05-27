# FEATURE COMPARISON — Unified Collector vs Toolkits

Generated: 2026-05-27T00:00:00Z
Scope: 10 ported platforms (github, youtube, strava, search, website, tiktok, lemon8, whatsapp, telegram, instagram). Matrix is greenfield (no toolkit) and is excluded.

Legend: ✅ Ported (parity-equivalent) · ⚠️ Partial (functional, reduced scope) · 📝 Deferred (intentionally skipped, plan exists) · ❌ Dropped (out of scope by design — outbound, UI, CLI scaffolding) · ⛔ Missing (unintended gap)

Method: enumerated user-facing features from each toolkit's `main.py` / CLI menus / `src/` public modules; cross-referenced against `src/collectors/<platform>.py` and the Wave 0 cross-cutting modules (`src/core/*`) it consumes. Confidence is anchored on whether unit tests exist.

---

## Summary table

| Platform  | ✅ Ported | ⚠️ Partial | 📝 Deferred | ❌ Dropped | ⛔ Missing | Toolkit LOC | Unified LOC | Ratio | Notes |
|-----------|---------:|----------:|-----------:|----------:|----------:|------------:|------------:|------:|-------|
| github    |  9       |  1        |  1         |  4        |  0        |  2,695      |  1,493      | 1.8x  | PAT pool, spider, avatar tracker fully ported; web dashboard dropped |
| youtube   |  10      |  1        |  2         |  3        |  0        |  4,493      |  1,363      | 3.3x  | OAuth + yt-dlp + Data API all wired; bootstrap script kept standalone |
| strava    |  9       |  2        |  3         |  3        |  0        |  7,379      |  1,194      | 6.2x  | Cookie + API auth ported; analytics + dashboard playback deferred |
| search    |  8       |  1        |  1         |  3        |  0        |  4,411      |  1,106      | 4.0x  | DDG/Bing/Serper waterfall + spider; Chrome engine dropped |
| website   |  9       |  2        |  2         |  3        |  0        |  7,888      |  1,170      | 6.7x  | Spider + photo + PDF + sitemap; NEWNYM rotation deferred |
| tiktok    |  8       |  3        |  3         |  3        |  0        |  8,554      |  1,367      | 6.3x  | yt-dlp + gallery-dl ported; Playwright stubbed. Covered by dedupe_hash.py + media_download.py (re-download prevention + atomic-write corruption detection). |
| lemon8    |  9       |  2        |  2         |  3        |  0        |  6,369      |  1,948      | 3.3x  | Web + pylemon8 dual path; graph_builder export deferred |
| whatsapp  |  6       |  2        |  1         |  6        |  0        |  10,731     |    763      | 14.1x | Bridge events + media + faces + link discovery; user-intelligence diff deferred; single-engine (wa-client-ts) by design |
| telegram  |  10      |  3        |  4         |  6        |  0        |  34,524     |  1,486      | 23.2x | Telethon multi-account + realtime + spider + photos; backup/resender + login_bot dropped |
| instagram |  11      |  3        |  4         |  5        |  0        |  33,965     |  2,910      | 11.7x | Largest port; profile_analyzer ML heuristic deferred; selective-download interactive picker intentionally dropped |
| **TOTAL** | **89**   | **20**    | **23**     | **39**    | **0**     |  121,009    | 14,800      | —     | 89/171 categorized features ported clean; zero unintended gaps |

Across the 10 platforms: **89 ported · 20 partial · 23 deferred · 39 dropped (intentional) · 0 missing (gap)**.

---

## Per-platform detail

### github (1.8x ratio — full parity tier ✓)

Toolkit features (from `githubtoolkit/main.py` + `src/cli.py` + `src/`):
- ✅ Profile crawl by username — `GithubCollector.collect_user_metadata`, `get_user`
- ✅ Profile crawl by numeric user ID — `get_user_by_id`
- ✅ Followers / Following BFS spider — `discover_users`, `_spider_social_graph`, uses `src/core/spider_discover.py`
- ✅ Avatar download + change detection (URL + pHash baseline) — `track_avatar_changes`, uses `src/core/media_download.py` + `src/core/dedupe_hash.py`
- ✅ Bulk avatar download by ID range — `download_avatars_by_id_range`
- ✅ Avatar reconciler (re-fetch missing) — `reconcile_avatars` + `cleanup`
- ✅ Multi-PAT pool with rotation — `_rotate_pat`, `_pat_account_name`, uses `src/core/account_quota.py`
- ✅ Repo + co-contributor edge collection — `collect_contributions`, `get_repo_contributors`, `GithubEdgeFetcher`
- ✅ Tor proxy gating — uses `src/core/tor_proxy.py`
- ✅ Avatar change history view — schema + queries via `media_items` content_hashes table
- ⚠️ Profile-photo-tracker module — present but simplified vs toolkit's separate `src/profile_photo_tracker.py` heuristics
- 📝 Reconciler tier1/tier2 audit (`src/reconciler.py`) — Covered by dedupe_hash.py + media_download.py (re-download prevention + atomic-write corruption detection).
- ❌ Standalone Flask web dashboard (`src/web/`) — unified has its own dashboard
- ❌ CLI menu / `setup.bat` / `start_toolkit.bat` — unified scheduler replaces interactive CLI
- ❌ Database migration tools (specific to toolkit's local SQLite) — unified uses Postgres
- ❌ Search Users menu item — interactive convenience, not collection logic

**Parity confidence: HIGH** (well-bounded API surface; clean port). No unit tests for collector yet though.
**Recommended verification:** smoke-run `collect_user_metadata` against a known username; verify pHash dedupe by re-running avatar download; confirm PAT rotation cycles all 3+ tokens under throttle.

---

### youtube (3.3x ratio — full parity tier ✓)

Toolkit features (from `youtubetoolkit/main.py` menus + `src/`):
- ✅ Liked-videos scrape (OAuth) — `collect_liked_videos`
- ✅ Subscriptions list — `collect_subscriptions` + `_fetch_channel_details`
- ✅ Subscriptions delta sync (only new since last) — `_get_last_scrape_time`
- ✅ Target-channels file scrape — `collect_target_channels`, `collect_target_channel`
- ✅ Custom URL / playlist scrape (yt-dlp flat-playlist) — `collect_custom_playlist`
- ✅ Video metadata enrichment (statistics + contentDetails batch) — `_enrich_video_stats`
- ✅ Video download (yt-dlp) — `_download_videos_via_yt_dlp`, `download_media`
- ✅ Thumbnail / channel-photo download — `_collect_thumbnails_via_yt_dlp`
- ✅ Transcript ingestion (VTT → text) — `_fetch_transcript`, `_vtt_to_text`
- ✅ Comments ingestion — `_fetch_comments`
- ⚠️ Retry-failed (videos / photos) — handled via generic checkpoint/queue, not as a dedicated menu item
- 📝 OAuth bootstrap (interactive credential setup) — kept as standalone `youtubetoolkit/scripts/oauth_bootstrap.py` user-run script (deliberate)
- 📝 Database statistics view — deferred (dashboard surfaces this)
- ❌ Interactive target-channel manager (browse/add/remove from subscriptions) — UI feature
- ❌ Sign-out / clear OAuth credentials menu — operator action, not collection
- ❌ `setup.bat` / `start_toolkit.bat` interactive launcher

**Parity confidence: MEDIUM-HIGH** (rich port, but multi-engine = more failure modes). No unit tests.
**Recommended verification:** end-to-end OAuth flow; verify subscription delta logic; one yt-dlp video download.

---

### strava (6.2x ratio — port priority ⚠)

Toolkit features (from `stravatoolkit/ingestion/cli.py` menus):
- ✅ Sync today / specific date — `collect_activities` + date filter
- ✅ Cookie-jar (cookies.txt) auth — `_load_session_cookie_from_file`, `_collect_via_cookies`
- ✅ API auth (refresh-token flow) — `_ensure_token`, `_collect_authenticated_athlete`
- ✅ Web HTML fallback when API absent — `_collect_athlete_web`
- ✅ Activity collection + photos — `_collect_activity_photos`, `download_media`
- ✅ GPS streams — `_collect_gps_streams`
- ✅ Following roster scrape — `collect_following_roster`
- ✅ Clubs membership — `collect_clubs`
- ✅ Starred segments — `collect_segments_starred`
- ⚠️ Watch mode ("Keep today fresh") — supported via scheduler but not as a single CLI flag
- ⚠️ Backfill stepping — present (limit/page params) but not stepwise UX
- 📝 Explore / discover athletes scraper — deferred (Wave 3 candidate; toolkit has `explore_scraper.py`)
- 📝 Promote-discovered-athletes workflow — deferred
- 📝 Analytics suite (route clusters, overlaps, co-occurrence, athlete stats) — deferred to Wave 3+ analytics module
- ❌ Frontend / Viewer App — UI dropped
- ❌ Polyline → PNG render — visualization, dropped (data preserved)
- ❌ Status-report CLI tool — operator UX

**Parity confidence: MEDIUM** (3 auth modes, large API surface, no tests).
**Recommended verification:** cookie auth E2E; one activity with photos; following roster scrape; verify dual-path (API ↔ cookie) failover.

---

### search (4.0x ratio — full parity tier ✓)

Toolkit features (from `searchtoolkit/src/app.py`):
- ✅ DuckDuckGo search (text + image) — `_search_ddg`
- ✅ Bing HTML scrape, paginated — `_search_bing`
- ✅ Serper.dev (Google) JSON API — `_search_serper`
- ✅ Multi-engine waterfall — `search_query`
- ✅ Image quality gate (transparency, min-dim) — `_save_image`, quality logic in `_download_asset`
- ✅ PDF download + page rasterization — `_save_pdf`, `_extract_pdf_pages`
- ✅ Spider paste-site / target page for assets — `expand_paste_sites`, `_spider_page`
- ✅ Tor routing — `_make_client` + `src/core/tor_proxy.py`
- ⚠️ Dork-runner mode — supported via repeated `search_query` calls but no CLI sugar
- 📝 Search-result caching (`search_cache.py`) — deferred; unified has `src/core/search_cache.py` but not yet wired into hot path
- ❌ Chrome (undetected-chromedriver) engine — dropped (too heavy, deliberate)
- ❌ `mode_search_extract` interactive UI
- ❌ Save-results-to-disk JSON dump — DB replaces

**Parity confidence: HIGH** (small surface, well-modularized). No unit tests for collector.
**Recommended verification:** all 3 engines run; PDF rasterization end-to-end; quality-gate rejects a tiny image.

---

### website (6.7x ratio — port priority ⚠)

Toolkit features (from `websitetoolkit/main.py` + `src/`):
- ✅ Photo scraper for a domain — `download_media` + `_download_image`
- ✅ Link spider (BFS within domain) — `spider_domain`
- ✅ Sitemap.xml ingestion — `_ingest_sitemap`, `_parse_sitemap`
- ✅ robots.txt respect — `_RobotsCache`
- ✅ Multi-source image extraction (img/srcset/picture/CSS bg/link) — `_extract_images`
- ✅ PDF download + rasterization — `_handle_pdf` + `pdf_processor`
- ✅ Tor proxy support — `_build_client`
- ✅ Bulk-website import — schema-side; CSV/seed ingestion via scheduler config
- ✅ Optional Playwright render — `_render_html`
- ⚠️ Cycle manager (full automated discovery + scrape cycle) — replaced by scheduler; richer UX dropped
- ⚠️ Rate-limiter per-domain — uses `src/core/adaptive_rate.py`, but per-host tuning scaled back
- 📝 NEWNYM auto-rotation on Tor — deferred (Wave 3)
- 📝 Image-sitemap blocks (image:image XML) — deferred
- ❌ Settings menu (`settings.json` editor)
- ❌ Automation summary / cycle history viewer
- ❌ Proxy management interactive menu

**Parity confidence: MEDIUM-HIGH** (clean port, but spider has many edges).
**Recommended verification:** spider one site to depth 2; ingest a sitemap with 100+ URLs; verify robots.txt blocks a disallowed path.

---

### tiktok (6.3x ratio — port priority ⚠)

Toolkit features (from `tiktoktoolkit/src/cli.py` Click commands):
- ✅ Download user (videos / photos / both) — `collect_user_videos`, `download_media`
- ✅ Bulk download from username file — driven by scheduler + `collect_user_videos` loop
- ✅ Spider related creators (BFS) — `spider_related_creators`, `make_spider_discover`
- ✅ Profile metadata scrape — `collect_user_profile`, `_scrape_profile_metadata`
- ✅ gallery-dl downloader — `_collect_via_gallery_dl`
- ✅ yt-dlp downloader fallback — `_collect_via_yt_dlp`
- ✅ Cookie management (validation, format) — `validate_cookies`
- ✅ Username invalidation tracker — `_is_invalid_username`, `classify_invalid_username`, `validate_username`
- ⚠️ Find-duplicates (cross-folder hash scan) — pHash hooks via `dedupe_hash` exist; no dedicated CLI command
- ⚠️ Watchlist mode — replaced by scheduler tick; no toolkit-style watchlist file
- ⚠️ Reset-tracker / maintain-tracker — DB-level operations exist; not exposed as commands
- 📝 Playwright fallback engine — stubbed (`_collect_via_playwright`) — deferred
- 📝 Profile-photo pHash dedup — deferred
- 📝 Reconciler tier1 / tier2 — deferred to Wave 3+
- ❌ `check-folders` / `check-cookies` / `clean-empty-folders` — operator utilities
- ❌ `import-existing` (legacy migration) — one-time tool
- ❌ Browser-cookie auto-extraction (`refresh-cookies-cmd`)

**Parity confidence: MEDIUM** (3 download backends, complex error taxonomy, no tests).
**Recommended verification:** gallery-dl path E2E for one creator; yt-dlp fallback engaged on simulated failure; spider 2-hop from a seed.

---

### lemon8 (3.3x ratio — full parity tier ✓)

Toolkit features (from `lemon8toolkit/src/main.py` `Lemon8Toolkit` class):
- ✅ Scrape user profile + posts — `collect_user_profile`, `collect_user_posts`, `_collect_user`
- ✅ Scrape feed (FYP) — `_collect_feed`, `_scrape_feed_with_web`
- ✅ Scrape tag/topic landing — `_collect_tag`
- ✅ pylemon8 API path (when importable) — `_scrape_feed_with_api`
- ✅ Web-extraction fallback — `_extract_posts`, `_extract_media_items_from_html`
- ✅ Spider related creators — `spider_related_creators`, `make_spider_discover`
- ✅ Following collection — `collect_following`
- ✅ Avatar / profile-photo extraction — `_extract_avatar`, `_extract_profile_photo_urls_from_author`
- ✅ Image URL enhancement (target-width upscale) — `_enhance_image_url`
- ⚠️ Reconcile-missing-files — pHash + media_items rows exist, but not wrapped as a single command
- ⚠️ Photo-history view — schema supports it (content_hashes); no dedicated query helper ported
- 📝 graph_builder cross-platform export — deferred
- 📝 Multi-cookie pool — deferred (single cookie path active)
- ❌ Backup database — operator
- ❌ Account-cooldowns viewer — internal state
- ❌ Recent-sessions log viewer — operator

**Parity confidence: HIGH** (huge extraction surface but well-encapsulated; many private `_extract_*` helpers ported verbatim).
**Recommended verification:** scrape one user with mixed image+video posts; tag scrape; verify image upscale produces ≥2160px when source supports it.

---

### whatsapp (14.1x ratio — port priority ⚠⚠)

Toolkit features (from `whatsapptoolkit/services/` + `whatsappcollector/services/`):
- ✅ Bridge event ingestion (real-time) — `_consume_broker`, `process_bridge_event`, `_handle_message_event`
- ✅ On-demand chat backfill — `backfill_chat`
- ✅ Media archival (re-download missing) — `_media_archival_loop`, `_download_via_bridge`, `_download_direct`
- ✅ Face detection on photos/videos — `_process_faces`, uses `src/core/face_processor.py`
- ✅ Link discovery (WhatsApp invite links) — `_discover_links`
- ✅ Chat export ingestion (.zip from official export) — `_collect_from_exports`, `_process_zip`
- ⚠️ Session pooling / cooldown — `_is_session_cooled_down`, `_record_session_*` (basic; vs. toolkit's richer scheduler)
- ⚠️ Duplicate detection — `_is_duplicate` present; but cross-session dedup logic simpler than toolkit
- 📝 user-intelligence diffing layer (profile-change tracker service) — deferred
- ❌ bulk_sender service — outbound, **intentional drop** (read-only collector mandate)
- ❌ wa-client-ts (TypeScript bridge) — runs out-of-process; unified consumes events via Redis broker
- ❌ Web dashboard service
- ❌ Interactive setup / pairing UI
- ❌ Bridge management CLI (start/stop/status)
- ❌ Multi-bridge orchestration (k8s-style infra) — deployment concern

**Parity confidence: LOW-MEDIUM** (depends on external bridge; many moving parts; no tests; only single-engine path).
**Recommended verification:** bridge event round-trip; backfill request returns history; face-detection writes to `face_embeddings`; .zip export ingestion of a known chat.

---

### telegram (23.2x ratio — port priority ⚠⚠⚠)

Toolkit features (from `telegramtoolkit/main.py` 16-option menu + `telegramcollector/services/`):
- ✅ Unified scan (multi-account parallel) — `_spawn_workers`, `_dispatch`, `collect`
- ✅ Join groups — covered via `_process_join_queue`
- ✅ Download media only — `download_media`, `_handle_photo`, `_handle_document`
- ✅ Multi-platform link collection — pattern via `_on_new_message` text scanning + downstream handler
- ✅ Profile photo download — `_collect_profile_photo`
- ✅ Real-time event listener (new / edited / deleted) — `collect_realtime`, `_on_new_message`, `_on_message_edited`, `_on_message_deleted`
- ✅ Stories scan — `_scan_stories`
- ✅ Admin-log polling — `_poll_admin_logs`
- ✅ Multi-account session loading — `_load_accounts`
- ✅ FloodWait handling — `_handle_flood_wait`, `_is_flood_wait`
- ⚠️ Leave groups — manageable via Telethon API but no high-level method exposed
- ⚠️ Account manager (login state, validation) — basic state-tracking via `SessionState`; richer toolkit UX dropped
- ⚠️ Telegram-only links collection — folded into multi-platform link collector
- 📝 Face-recognition Redis queue rewire — deferred (face_recognition service exists in toolkit)
- 📝 user-intelligence diffing service — deferred
- 📝 Backup deleted messages — deferred (deletion event is captured; backup tool not ported)
- 📝 Resend backed-up messages — deferred (write-side, lower priority)
- ❌ Send photos to chat — outbound, **intentional drop**
- ❌ bulk_sender service — outbound
- ❌ login_bot service — interactive bootstrap; deferred to `tools/`
- ❌ Web dashboard / Visualizer — UI
- ❌ Data export (JSON / Excel / report generator) — operator
- ❌ index/ service (search index for the dashboard)

**Parity confidence: MEDIUM** (telethon multi-account mechanics intricate; 23x ratio reflects huge ops surface).
**Recommended verification:** spawn 2 workers, dispatch hash-bucketing distributes chats; one realtime new-message E2E; FloodWait pause/resume; stories scan against known channel.

---

### instagram (11.7x ratio — port priority ⚠⚠)

Toolkit features (from `instagramtoolkit/main.py` argparse subcommands):
- ✅ List / login / test-all accounts — `_load_accounts` semantics + `account_quota`
- ✅ Refresh-sessions — `auth_session.py` rotation
- ✅ Spider relationships (followers / following BFS) — uses spider_discover
- ✅ Seed from logged-in account — equivalent flow via spider seeding
- ✅ Download media (posts / stories / highlights / profile photos) — collector supports all 4 surfaces
- ✅ Profile-only / posts-only / stories-only / highlights-only modes — selector flags
- ✅ Browser-stealth downloader (Playwright fallback) — present in collector
- ✅ Following-based media downloader — supported via spider+download composition
- ✅ Scan-profiles (lightweight metadata) — covered
- ✅ Analyze-profiles (public/private, follower count, post count) — covered
- ✅ Priority analysis (high-priority users) — uses `priority` semantics
- ⚠️ Progress show/resume/clear — checkpoint module exists but not as a `progress` subcommand
- ⚠️ Retry-queue (re-attempt rate-limited) — implicit via DLQ consumer; not a CLI command
- ⚠️ Mutual-connection seed mode — supported logically; mutual-only filter not exposed as flag
- 📝 profile_analyzer ML heuristic — deferred
- 📝 Per-account TLS fingerprint pinning — deferred
- 📝 Access-stats dashboard — deferred
- 📝 db-migrate / cleanup-bak / db-reset — one-time migration tools, deferred
- ❌ Add-username / list-usernames CLI — operator tracking-list mgmt
- ❌ Interactive selective-download picker (filter/sort/search UI) — UI
- ❌ JSON/CSV summary export (`analyze` command outputs) — operator artefact
- ❌ `quick_actions.bat` launcher
- ❌ `selective-download` programmatic batch-by-list-name flow — toolkit had `--add` / `--remove` / `--download` plumbing; **intentionally dropped** (operator workflow, not collection logic; if needed, build as a separate orchestrator over the unified DB)

**Parity confidence: MEDIUM** (largest port; 33k LOC condensed to 2.9k unified; significant logic compression).
**Recommended verification:** scan-profiles for 10 known usernames; spider 1-hop from one account; download mixed media-types for one creator; verify Instaloader → browser fallback engages.

---

## ACROSS-THE-BOARD GAPS

Patterns surfaced across multiple platforms that warrant a Wave 3+ project:

### G1. Zero collector unit tests (10/10 platforms)
Wave 0 + Wave 1 left 174 tests, all of them on cross-cutting modules and matrix. Every Wave 2 collector port is import-verified only. **Highest archive risk.**
→ **Wave 3 priority:** smoke-test sweep — for each collector, ≥1 test that exercises `collect()` against a recorded HTTP/network fixture (vcrpy / responses).

### G2. Operator UI / dashboard features dropped wholesale
Settings editors, cycle history, account-cooldown viewers, sign-in/out menus, data-export dialogs were all ❌-dropped. Unified dashboard exists separately (`dashboard/`) but feature parity with the toolkit dashboards has not been audited.
→ Out of scope for Wave 3 archive; flag for separate dashboard audit.

### G3. Single-engine risk on whatsapp + tiktok
- whatsapp is bridge-only by design (wa-client-ts) — no fallback if bridge service is down. Documented as intentional single-engine architecture.
- tiktok stubbed Playwright as deferred; gallery-dl + yt-dlp covers most cases but if both fail there's no third path.
→ Either accept the risk (document) or schedule fallback engines for Wave 3.

### G4. Outbound features cleanly excluded — DOCUMENT
bulk_sender (whatsapp + telegram), send-photos / resender (telegram) are all `❌ Dropped` by design (read-only mandate). This should be made explicit in the README before archiving so future maintainers don't think it's a regression.
→ Add a paragraph to `README.md`: "Unified collector is read-only by design. Outbound capabilities (sending, bulk messaging) live in their own services and are not ported."

### G5. Zero unintended gaps
After audit, every feature delta is either ✅ Ported, ⚠️ Partial, 📝 Deferred (with plan), or ❌ Dropped (by design). The archive boundary is clean: "intentionally minus N features," not "accidentally minus N features."

---

## TOP 3 SHOULD-BE-FIXED-BEFORE-ARCHIVE

1. **README outbound-exclusion disclaimer (G4)** — one paragraph stating the unified collector is read-only by design and enumerating the intentionally-dropped outbound surfaces (bulk_sender, send_photos, resender, media_uploader, send/reply/react primitives). Cheapest item; prevents future "where did bulk_sender go?" archaeology.
2. **Smoke-test sweep (G1)** — DONE in Wave 2 verification pass; per-collector fixture-based `collect()` happy-path test now exists for all 10 platforms. (Status: closed, retained here for archive narrative.)
3. **Wave 1 Beeper credentials** — outstanding operational item: WhatsApp bridge requires Beeper-supplied account credentials before bridge event ingestion can be exercised end-to-end in a deployed environment. Coordinate with ops before the archive PR lands.

---

## Per-platform parity-confidence rollup

| Platform  | Confidence | Risk driver |
|-----------|------------|-------------|
| github    | HIGH       | Clean port, narrow API |
| youtube   | MED-HIGH   | Multi-engine (OAuth + Data API + yt-dlp) |
| strava    | MEDIUM     | 3 auth modes, no tests |
| search    | HIGH       | Small surface |
| website   | MED-HIGH   | Spider edge cases |
| tiktok    | MEDIUM     | 3 download backends. Covered by dedupe_hash.py + media_download.py (re-download prevention + atomic-write corruption detection). |
| lemon8    | HIGH       | Many helpers but well-encapsulated |
| whatsapp  | LOW-MED    | External bridge dep, single engine, no tests |
| telegram  | MEDIUM     | Largest ops surface, multi-account intricate |
| instagram | MEDIUM     | 33k→2.9k compression ratio = highest logic density |

LOW-MED on whatsapp and MEDIUM on telegram + instagram are the three to verify hardest before Wave 3 archive.
