# Collection Specification — Capture Standard for All Collectors

> **Status:** authoritative. Every collector implements this standard; each adapts it to
> what its platform actually exposes (see the applicability matrix). **When adding a NEW
> source, implement this spec and add a row to the matrix.** Defined by the project owner
> 2026-06-13. Future LLM agents: treat this as requirements, not suggestions.

## Capture priority (process in this order when the ban/IP budget is tight)

1. **Ephemeral** — expires in ~24h: Instagram/Telegram/TikTok Stories, WhatsApp Status.
2. **Media** — photos, videos, message bubbles (text).
3. **Documents & audio**.
4. **Profile content** — bio, profile photo, creation date, memberships, comments, reactions.
5. **Location** — GPS, shared/live location, EXIF.
6. **Everything else** — links, polls, pinned, events.

Scheduling MUST honour this tier order (ephemeral first).

---

## Locked requirements (apply per collector)

### Tier 1 — Ephemeral / Stories
- Poll **only stories visible to our logged-in accounts** (use the follow-aware multi-account
  access model: pick an account that can see the target; private targets only if an account follows them).
- Poll cadence: **every 4 hours** per target.
- **Reuse the same collection accounts** (no dedicated/isolated story accounts).
- **All platforms in v1** (those that have stories: instagram, telegram, tiktok, whatsapp-status).
- **Storage:** stories are media → rows in the shared `media_items` table with
  `content_type='story'` / `'story_video'`, tagged by `source`, file on disk. **No separate schema.**

### Tier 2 — Media
- **Highest quality always** (HD photos, best video stream).
- **Download full** — no size cap (download large videos in full).
- Edits/deletes: **track edits + mark deletions — DEFERRED** (not now; first get all content into DB).

### Tier 3 — Documents & audio
- Documents: **whitelist safe types only** — PDF, Word, PowerPoint, Office, images, text.
  **Skip executables and code files.**
- Audio: **store the file** (no transcription, except YouTube where transcripts are already available).
- Stickers: **collect static, skip animated** (.tgs/.webm).

#### Website & search spider file policy (implemented)
The `website` and `search` collectors crawl the open web, so their download
policy is an explicit allow/deny by extension + content-type:
- **Download:** images, PDF (with page rasterisation), office/text documents
  (`.doc/.docx/.xls/.xlsx/.ppt/.pptx/.txt/.rtf/.csv/.odt/.ods/.odp`), and
  **videos** (`.mp4/.mov/.webm/.mkv/.avi/.m4v/.mpeg/.mpg/.wmv/.flv/.ogv/.3gp`).
- **Never download:** audio (`.mp3/.wav/.m4a/.ogg/.flac/...`), code &
  executables (`.js/.py/.exe/.dll/.jar/...`), and html/static assets
  (`.css/.woff/.ico/...`). These are folded into the crawler skip-lists so
  they're neither fetched nor queued.
- **Videos have no size cap** (streamed to disk in chunks, never buffered into
  memory). Documents keep a 50 MB cap. Toggles/caps:
  `WEBSITE_DOWNLOAD_DOCS/VIDEOS`, `WEBSITE_MAX_DOC_BYTES`,
  `WEBSITE_MAX_VIDEO_BYTES` (0 = uncapped) and the `SEARCH_*` equivalents.
- `content_type` written: `image`, `pdf`, `document`, `video`.

### Tier 4 — Profile content
- **Full change history** — bio history, username changes, profile-pic history (pHash-based).
- Collect **comments AND reactions** (post comments, message/post reactions).
- Collect **memberships** (groups/channels joined) **and spider the follower/following graph** for discovery.

### Tier 5 — Location
- **Extract EXIF GPS** from downloaded photos/videos (best-effort; social platforms often strip metadata).
- **Parse shared/live-location messages** (telegram/whatsapp) into structured coordinates.

### Tier 6 — Everything else
- **Extract & resolve links** from message text / bios / descriptions → feed the spider (cross-platform discovery).
- Collect **polls, pinned messages, and events/RSVP**.

### Cross-cutting
- **Backfill depth: everything available** (full message/post history, all media).
- **Tier-ordered scheduling** (see priority above).
- Dedup via sha256 (existing `media_items` pattern); files on disk, metadata in Postgres.

---

## Per-collector applicability matrix

Legend: ✅ supported/required · ➖ N/A for platform · 🔲 to build

| Tier / feature | instagram | telegram | tiktok | youtube | strava | lemon8 | website | github | whatsapp | beeper |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 Stories/ephemeral | ✅ | ✅ | 🔲 | ➖ | ➖ | ➖ | ➖ | ➖ | ✅(status) | ➖ |
| 2 Media (photo/video) | ✅ | ✅ | ✅ | ✅ | ✅(activity) | ✅ | ✅(img+video) | ➖(avatars) | ✅ | ✅ |
| 2 Message bubbles | ➖ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ✅ | ✅ |
| 3 Documents | ➖ | ✅ | ➖ | ➖ | ➖ | ➖ | ✅(pdf+office) | ➖ | ✅ | ✅ |
| 3 Audio (store) | ➖ | ✅ | ➖ | ✅(file) | ➖ | ➖ | ➖ | ➖ | ✅ | ✅ |
| 4 Profile + change history | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ➖ | ✅ | ✅ | ✅ |
| 4 Comments/reactions | ✅ | ✅ | ✅ | ✅ | ✅(kudos/comments) | ✅ | ➖ | ➖ | ✅ | ✅ |
| 4 Memberships + graph spider | ✅ | ✅ | ✅ | ✅(subs) | ✅(follows) | ✅ | ➖ | ✅(contrib) | ✅ | ✅ |
| 5 Platform geotags | ✅ | ✅ | ✅ | ➖ | ✅(GPS) | ✅ | ➖ | ➖ | ✅(loc msg) | ✅ |
| 5 EXIF GPS from media | ✅ | ✅ | ✅ | ➖ | ➖ | ✅ | ✅ | ➖ | ✅ | ✅ |
| 6 Links | ✅ | ✅ | ✅ | ✅(desc) | ➖ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 6 Polls/pinned/events | ➖ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ✅ | ✅ |

_(Adjust cells as platform capabilities are confirmed during implementation.)_

> **Note:** ✅ above means "required/planned," NOT "shipped." See the audit below.

## Implementation status — audited 2026-07-12

A codebase + live-DB audit found the reusable **Phase 0 building blocks already
exist as code**, but several are not yet wired, and Tier 1 is not yet flowing:

- **Follow-aware account selector** — `src/core/profile_access.py`
  (`ProfileAccessRepository` + `SmartAccountSelector`, source-agnostic; this
  supersedes the single-purpose `instagram_account_access` table the plan named).
  Tables `profile_access_summary` / `profile_access_attempts` exist but are
  **EMPTY (0 rows)** — only `whatsapp` imports it; instagram/tiktok/lemon8 do not
  record attempts yet. **Gap: wire `record_attempt()` into the private-target
  collectors, then route via `select_for_operation()`.**
- **EXIF GPS** — `src/core/exif_gps.py` exists; verify it's called in the
  media-download pipeline (Tier 5).
- **Link extractor** — `src/core/link_extractor.py` exists.
- **Change tracking / reconciler** — `src/core/{change_tracker,user_change_tracker,
  reconciler,identity_reconcile}.py` exist.
- **Media-type policy** — `src/core/{media_filter,document_filter}.py` +
  the website/search allow-deny policy shipped 2026-07-12.
- **Tier 1 Ephemeral/Stories — PARTIALLY working (correction 2026-07-13).** An
  earlier note here said "not flowing" — that was a MEASUREMENT ERROR (only
  `content_type` was checked). Ephemeral media is stored under the
  **`media_items.kind`** column, not `content_type`. Instagram IS capturing it
  via the extension -> ig_ingest `/social/ingest` path (`kind` in
  {post, story, highlight, tagged, profile}, namespaced dedup ids `story_`/`hl_`):
  as of 2026-07-13, instagram has **story=100, highlight=3,658** rows (plus
  post=80,416, tagged=33,735).

  **Stories rollout (2026-07-13):**
  - ✅ Dashboard Stories view shipped (`/stories`, queries `media_items.kind`).
  - ✅ Storage unified onto `media_items.kind='story'` — `insert_media_item`
    gained a `kind` param; whatsapp `status@broadcast` media and telegram
    `_scan_stories` output are now tagged `kind='story'` (were untagged /
    `content_type='story'`).
  - ✅ Telegram `_scan_stories` now resolves each chat via its OWNING account
    (`_resolve_entity_any_worker`) — the single-account resolution was why it
    produced 0 rows ("Cannot find any entity").
  - ❌ **TikTok stories — NOT FEASIBLE (spiked 2026-07-13).** Neither yt-dlp
    (2026.03.17) nor gallery-dl (1.32.1) has a TikTok *story* extractor (only
    user/post/live/collection/avatar/following/likes), and TikTok has largely
    deprecated standalone Stories. Would require reverse-engineering the private
    story API — high ban-risk, low value. Skipped.
  - Note: Instagram/WhatsApp/Telegram story capture is PASSIVE (extension
    in-tab / status broadcasts / 300s scan) — it captures what's seen, not an
    exhaustive active poll. That's why counts are modest + rolling (stories are
    24h-ephemeral); highlights persist so they accumulate.

Working & verified: Tier 2 media (all sources download files), Tier 3 docs/audio
(telegram/beeper + website/search office-docs & video as of 2026-07-12).

---

## Storage conventions
- **All media** → shared `media_items` table (`source`, `entity_id`, `content_type`, `content_id`,
  `filename`, `file_path`, `file_size`, `sha256`, `metadata` jsonb, `collected_at`). Files on the media volume.
- `content_type` values include: `image`, `video`, `photo`, `story`, `story_video`, `profile_photo`,
  `document`, `audio`, `sticker`, `thumbnail`, `activity_photo`, `pdf`, `post`.
- Structured (non-media) data → per-platform tables (`<source>_posts`, `<source>_messages`,
  `<source>_profiles`, reactions/polls/relationships, etc.).
- `metadata` jsonb columns: **always `json.dumps()`** the value (asyncpg has no dict→jsonb codec here —
  passing a raw dict silently fails the insert).

## Adding a new source
1. Implement each tier the platform supports (use the matrix; fill a new column).
2. Reuse `media_items` for all media (incl. stories) and `BaseCollector` patterns (dedup, backfill, media_download).
3. Honour the follow-aware account model and tier-ordered scheduling.
4. Update this spec's matrix.

---

## Implementation status — refreshed 2026-07-24

Current shipped recovery/ops work:

- **External vault fail-closed guard:** `src/core/vault.py::assert_media_write_allowed`
  verifies `/vault`, media-root placement, and the `/media` ↔ `/vault/media`
  relationship before normal collector writes. `BaseCollector.run()` and
  `BaseCollector.save_json()` use it. `BaseCollector.save_file()` now writes
  binary artifacts through the canonical sha256 blob writer instead of a
  legacy per-source file path.
- **Sidecars and raw payload helpers:** media sidecars, artifact sidecars, raw
  payload writes, and validation live in `src/core/vault.py`; `BaseCollector`
  and extension ingest record sidecar status into metadata/DLQ.
- **Shared atomic artifact writer:** `src/core/vault.py::write_atomic_artifact`
  writes bytes through vault temp storage, verifies checksum/size, moves to the
  canonical sha256 blob path, writes a sidecar, optionally calls a DB writer,
  and marks post-blob sidecar/DB failures as partial for repair. Duplicate-row
  cleanup preserves canonical `media/blobs/...` files and only removes legacy
  per-occurrence duplicate files. New Beeper, Telegram, WhatsApp, Lemon8,
  YouTube, TikTok, Website, Search, Instagram headless/extension media,
  GitHub direct media, GitHub bulk avatar artifacts, shared profile-photo, and
  Strava activity photo/route-map downloads use this path for physical blobs
  while preserving each source occurrence as its own `media_items` row and
  media sidecar where a concrete source occurrence should be indexed. TikTok
  Playwright fallback writes only vault-temp intermediates and removes them
  after the collector re-ingests the bytes into canonical blobs. The shared
  `src/core/media_download.py` HTTP/delegated single-file helper also streams to
  vault temp and commits to canonical sha256 blobs with artifact sidecars, so
  future callers do not reintroduce final per-source file writes.
- **Rebuild report:** `python -m src.main rebuild-report --compare-db
  --verify-checksums` reports sidecar coverage plus DB-only, sidecar-only,
  blob-only, missing-file, and checksum-mismatch states. DB comparison is
  bounded by `REBUILD_REPORT_DB_COMPARE_TIMEOUT_SECONDS` and JSON output keeps
  logs on stderr so automation can parse stdout directly.
- **Repair canonicalization:** the generic media reconciler re-download path
  writes recovered bytes through the same canonical sha256 blob writer and
  updates `media_items.file_path/file_size/sha256` plus `metadata.vault_artifact`
  to point at the repaired vault blob.
- **Raw Strava payloads:** authenticated Strava club memberships are archived
  with `write_raw_payload()` under `raw/strava/...` and marked as rebuild input
  for `strava_athletes`, instead of writing ad hoc JSON into the media tree.
  API activity pages, individual activities, and API/web GPS stream responses
  are also archived with rebuild hints for `strava_activities` and
  `strava_gps_streams`.
- **Browser-extension observability:** extension hooks are tracked through
  `dm_hook_heartbeat`; browser ingest requests are recorded in
  `browser_ingest_events` and shown in the hourly Telegram status as browser
  saw/stored/POST counts for the current UTC hour.
- **Tier 1 raw messaging payloads:** Telegram chat/user/message/profile
  payloads, WhatsApp bridge message/contact/delete events, and Beeper
  account/chat/participant/message shadow payloads are archived through
  `write_raw_payload()` with rebuild table hints, so these live messaging
  records are not rebuild-dependent on Postgres JSONB alone.
- **Tier 3 shared attachment policy:** Telegram, WhatsApp bridge media, and
  Beeper attachments now route arbitrary file/document/sticker/audio decisions
  through `src/core/document_filter.py`, so safe documents and static media are
  kept while executable/code-like files and disabled audio are skipped before
  download.
- **Dashboard media joins:** chat/video dashboards join media by stable source
  keys first (`wa_<message_id>`, YouTube thumbnail/video content IDs) and use
  file-path matches only as legacy fallback.
- **Rate-limit visibility:** recorded HTTP 429/auth events are split in dashboard
  and Telegram status. Instagram and Strava cooldowns are persisted and surfaced
  with source/account/scope. A shared pre-cooldown retry primitive now performs
  one randomized delayed retry before cooldown-capable paths escalate; Instagram
  Playwright profile fetches, Strava GPS stream fetches, and Search HTTP fetch
  paths use it. GitHub API quota exhaustion, which GitHub reports as HTTP 403,
  is stored as a rate-limit event with the real HTTP status preserved in metadata
  so hourly status/dashboard views treat it as quota pressure, not generic auth
  failure. Browser-captured Strava stream HTTP 429/401/403 responses are also
  recorded as durable events under `browser_strava_streams`.
- **Strava GPS routes:** existing stored `strava_gps_streams.latlng` rows are
  repaired into route fields without network calls; GPS stream 429 cooldown is
  restored after restart so the backfill does not hammer Strava again. Recovered
  stream 429s are logged as transient without creating an active cooldown; a
  second 429 still opens the scoped GPS stream cooldown. Strava 429 waits use
  the shared jittered sleep helper instead of fixed sleeps.
- **Chat shared/live locations:** Telegram message-location extraction is live
  (`telegram_message_locations`). WhatsApp bridge normalization now preserves
  Baileys `locationMessage` and `liveLocationMessage` events as
  `messages.location`, and the Python collector stores them in
  `whatsapp_message_locations` for analyzer map ingestion.
- **Live bounded probe:** On 2026-07-24, `rebuild-report --compare-db
  --compare-db-limit 200 --sidecar-limit 200 --blob-limit 200 --json` parsed as
  clean JSON, scanned 200 DB media rows and 200 sidecars, and returned
  `db_compare_error=null` within the 10s DB compare budget. The sample reported
  200 `website` rows as `db_only` plus `file_missing`, which is now measurable
  repair input instead of a hanging scan.

Known remaining gaps:

- Legacy audit/status docs may still overstate exhaustive rate-limit coverage;
  every new 429/FloodWait/quota branch must call `record_rate_limit_event()`.
- Pre-cooldown retry is wired into the active Instagram Playwright profile,
  Strava GPS stream, and Search HTTP hot paths. GitHub quota exhaustion now uses
  durable rate-limit events plus jittered sleep. Remaining 429/FloodWait branches
  should migrate to shared helpers as they are touched.
- Rebuild report is a dry-run report, not a full scratch DB rebuild.
- Tier 1 raw payload coverage is now strong for browser/extension captures,
  browser Strava streams, authenticated Strava clubs, Telegram/WhatsApp/Beeper
  messaging, Beeper shadow rooms, Strava API activity pages, individual Strava
  activities, Strava API/web GPS streams, and Instagram headless/httpx/Playwright
  profile and post payloads.
- Physical-file dedupe now has a core sha256 blob writer, and Beeper, Telegram,
  WhatsApp, Lemon8, YouTube, TikTok, Website, Search, Instagram
  headless/extension media, GitHub direct media, GitHub bulk avatar artifacts,
  shared profile-photo, generic `BaseCollector.save_file`, shared
  `media_download` HTTP/delegated single-file calls, and Strava activity
  photo/route-map downloads use it. No known high-volume collector media path
  remains on direct per-source file writes, but future source-local write paths
  found by audit should migrate to the same helpers. GitHub bulk avatar range
  writes artifact sidecars only and intentionally does not create `media_items`
  rows for unknown numeric IDs.
- External-drive loss behavior is strong for base collector writes, but each
  long-running realtime/direct file write path should continue moving toward
  the same shared guard instead of local ad hoc checks.
