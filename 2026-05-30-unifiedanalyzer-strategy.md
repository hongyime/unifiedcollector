# 2026-05-30 — UnifiedAnalyzer Strategy Session

**Date:** 2026-05-30  
**Context:** Defining the direction and scope of `unifiedanalyzer` — a companion project to `unifiedcollector`.  
**Status:** In progress — direction TBD

---

## 0. Hard Constraints (locked)

| Constraint | Decision |
|---|---|
| Paid APIs / API keys | ❌ None — all tools must be free and keyless |
| Repo structure | Separate repo (`unifiedanalyzer`), reads same PostgreSQL DB on external HDD |
| DB ownership | Collector writes, analyzer reads (read-only on collector tables) |
| UI | Web UI — FastAPI backend + React frontend (same pattern as unifiedcollector dashboard) |
| Entity resolution | Purely automatic — no manual tagging |
| Run mode | Scheduled — re-analyzes as new data arrives |

---

## 1. What unifiedcollector gives us (production-ready)

10 platforms. One PostgreSQL database. All structured.

### Data available per platform

| Platform | Rich Text | Identity Signals | Location | Media | Timestamps |
|---|---|---|---|---|---|
| **YouTube** | transcripts (full text), video descriptions, comments | channel name | — | videos, thumbnails | published_at, collected_at |
| **GitHub** | READMEs (full), issues (title+body), commit messages, user bio | email, real name, username | — | avatars | commit date, created/updated |
| **Website** | page content_text (full), meta, h1s, structured_data JSONB | — | — | images (with alt) | fetched_at |
| **Telegram** | messages, captions | username, first/last name, **phone number** | — | photos, videos, docs, audio | platform_created_at |
| **WhatsApp** | messages, quoted_text | name, pushname, **phone number** | — | images, video, audio | timestamp |
| **Instagram** | captions, comments, bio | username, full name, **email**, **phone**, external_url | **lat/lng per post** | posts, stories, highlights | platform_created_at |
| **TikTok** | descriptions, comments, music metadata | username, nickname, bio | — | videos, covers | create_time |
| **Strava** | activity descriptions | firstname, lastname, username | **full GPS trace** (latlng JSONB array), start/end coords | profile photos | start_date |
| **Lemon8** | post title+description | username, nickname, bio | location_name | images, video | collected_at |
| **Search** | snippets, page content | — | — | — | date_published |

### Universal spine

`media_items` — every downloaded file from every source in one table. Fields: `source, entity_id, entity_name, content_type, content_id, file_path, sha256, metadata JSONB`.

### What's already built in core/ (reusable by analyzer)

| Module | What it does | Reuse posture |
|---|---|---|
| `FaceProcessor` | dlib ResNet 128-dim embedding extraction from images + video frames (pHash dedup) | **Reuse as-is** — already source-agnostic |
| `FaceMatcher` | pgvector cosine L2, running centroid, auto-creates new identity on miss | **Reuse with schema rename** — currently `wa_face_*` tables, need generic `face_identities` / `face_embeddings` |
| `ProfilePhotoTracker` | 2-stage URL+pHash change detection, reads/writes `media_items.metadata` | **Reuse as-is** — already parameterized by `source` + `entity_id` |
| `ChangeTracker` | field-level diff log, currently writes to `wa_user_profiles` / `wa_user_history` | **Reuse with generalization** — parameterize table names |
| `LinkExtractor` | pure function — extracts WA group invites / contact links from text | **Reuse as-is** |
| `pdf_processor`, `url_filter` | PDF text/image extraction, URL normalization | **Reuse as-is** |
| `BaseCollector` lifecycle | checkpoint, circuit breaker, dedup, atomic file writes, DLQ | **Adapt pattern** for `BaseAnalyzer` |

---

## 2. What unifiedanalyzer needs to solve

unifiedcollector answers: *"collect data from platform X about entity Y."*  
unifiedanalyzer answers: *"what can we know about X from all the data we've collected?"*

The fundamental value is **cross-platform correlation** — connecting dots that exist in silos.

---

## 3. Strategic Directions Considered

### Direction A — Cross-Platform Identity Resolution Engine
**What:** Given a seed (username, email, phone, face), find and link all presence across all 10 platforms. Build a unified identity record with confidence scores.  
**Data used:** emails (GitHub, Instagram), phones (Telegram, Instagram), usernames (all platforms — fuzzy match), real names (GitHub, Strava, Telegram, WhatsApp), bio NLP similarity, face embeddings (extend from WA-only → all platforms).  
**Output:** `unified_identities` table — one row per person, cross-linked to platform profiles. Confidence score per link.  
**Infrastructure need:** NLP embedding model for bio similarity (sentence-transformers or similar); generalized FaceMatcher; pgvector already in place.  
**Complexity:** HIGH — fuzzy matching + NLP + face + disambiguation logic.  
**Viability:** HIGH — all raw signals are already in DB; face matching infra already exists.

---

### Direction B — Investigation / Timeline Engine
**What:** For a known target entity, reconstruct a unified chronological timeline of all their activity across every collected platform. Answer: "what was X doing in March 2026?"  
**Data used:** All platform tables with timestamps. Join on target_id → platform profile → posts/messages/activities.  
**Output:** `GET /timeline/{entity}?from=&to=` — merged, sorted events from all sources with platform tags.  
**Infrastructure need:** A cross-source entity resolver (just a mapping table), timeline query layer, API endpoint.  
**Complexity:** LOW-MEDIUM — mostly SQL + presentation.  
**Viability:** VERY HIGH — immediate value, no ML required to start.

---

### Direction C — Behavioral Intelligence / Profiling
**What:** For a target, derive behavioral patterns: posting schedule (activity heatmap by hour/day), sleep/wake inference, location patterns (Strava routes, Instagram post geolocations), topic preferences, engagement fingerprint.  
**Data used:** all timestamps → posting frequency; Strava GPS streams + Instagram lat/lng → movement; post text → topic clustering.  
**Output:** behavioral profile — activity heatmaps, frequent locations, topic distribution, engagement ratios.  
**Infrastructure need:** GPS unnesting (Strava JSONB arrays), timezone normalization, topic modeling (LDA or embedding clustering).  
**Complexity:** MEDIUM.  
**Viability:** HIGH — Strava GPS data alone is very rich; posting timestamps are trivially analyzable.

---

### Direction D — Content Intelligence / NLP Pipeline
**What:** Extract topics, entities, sentiment, and semantic similarity from all collected text. Detect topic trends over time. Find content reuse/plagiarism across platforms.  
**Data used:** YouTube transcripts (richest corpus), GitHub READMEs + issues, website page text, Telegram/WhatsApp messages, Instagram captions.  
**Output:** entity tags, sentiment scores, topic clusters, content similarity graph, trend detection.  
**Infrastructure need:** Embedding model + vector store (pgvector extension of existing DB); NLP pipeline (spaCy or transformers).  
**Complexity:** MEDIUM — standard NLP pattern, well-understood tooling.  
**Viability:** HIGH — YouTube transcripts + GitHub text are immediately usable.

---

### Direction E — Network / Graph Analytics
**What:** Build a social graph from follows, mentions, replies, forwards, hashtag co-occurrence across all platforms.  
**Data used:** `mentions TEXT[]` (Instagram, TikTok, Lemon8), reply threads (Telegram, WA, Instagram, TikTok, YouTube), `forward_from_chat_id` (Telegram), `hashtags TEXT[]`, GitHub contribution graph.  
**Output:** influence scores, community clusters, information propagation paths, key nodes.  
**Infrastructure need:** Graph lib (networkx for analysis).  
**Complexity:** MEDIUM-HIGH.  
**Viability:** MEDIUM — needs sufficient collected volume to be meaningful.

---

## 4. Recommended Architecture (regardless of direction)

```
unifiedanalyzer/
├── src/
│   ├── main.py                  # CLI: analyze --source X --target Y --from --to
│   ├── core/
│   │   ├── base_analyzer.py     # mirror of BaseCollector pattern
│   │   ├── face_matcher.py      # lifted + generalized from unifiedcollector
│   │   ├── face_processor.py    # reused as-is
│   │   ├── change_tracker.py    # generalized (parameterized tables)
│   │   ├── entity_resolver.py   # NEW: cross-platform identity linking
│   │   └── embedder.py          # NEW: text embedding model wrapper
│   ├── analyzers/               # one module per analysis type
│   │   ├── base.py
│   │   ├── timeline.py          # Direction B (start here)
│   │   ├── behavior.py          # Direction C
│   │   ├── identity.py          # Direction A
│   │   ├── content.py           # Direction D
│   │   └── graph.py             # Direction E
│   ├── db/
│   │   ├── connection.py        # shared asyncpg pool
│   │   └── schemas/
│   │       ├── analyzer.sql     # unified_identities, entity_mappings, analysis_runs
│   │       ├── face.sql         # generalized face_identities + face_embeddings
│   │       └── history.sql      # generic entity_history(source, entity_id, field, old, new)
│   └── api/
│       └── api.py               # FastAPI query layer
├── docker/
│   └── docker-compose.yml       # shares same postgres as unifiedcollector
└── requirements.txt
```

**Key architectural decision:** unifiedanalyzer uses its own database (`unifiedanalyzer`) on the same Postgres instance. It reads from `unifiedcollector` DB via a read-only connection and writes to its own DB. One Postgres instance, two databases, two concerns. Tables in the analyzer DB do NOT use the `ana_` prefix — they own their namespace.

---

## 5. Chosen Direction

> TBD — to be decided in session.

---

## 6. Recommended Build Sequence

1. **Start with Direction B (Timeline)** — immediate value, no ML, validates the cross-source entity model
2. **Layer Direction C (Behavioral)** — timestamp + GPS analysis, minimal extra infra
3. **Layer Direction D (NLP)** — add pgvector text embeddings, topic clustering
4. **Layer Direction A (Identity Resolution)** — hardest, built on the above
5. **Direction E (Graph)** — last, needs sufficient data volume

---

## 7. Open Questions / Roadblocks

- [ ] What is the primary USE CASE? Personal OSINT? Research? Monitoring known contacts?
- [ ] Single-target deep dive vs. fleet analysis across all collected entities?
- [ ] Real-time (as data arrives) vs. batch (scheduled runs)?
- [ ] Extend unifiedcollector's dashboard or separate UI?
- [ ] GPU available for face processing at scale?

---

## 8. Action Items

> TBD after direction is set.

---

## 9. Deep-Dive: Signal Analysis (from schema audit)

### Identity Resolution — Signal Tiers

| Tier | Signal | Source Table.Column | Confidence | Notes |
|---|---|---|---|---|
| **DIRECT** | Phone number | `telegram_users.phone` | 1.0 | Telegram requires real phone |
| **DIRECT** | Phone (encoded) | `whatsapp_users.platform_user_id` | 1.0 | JID = `{e164_phone}@s.whatsapp.net` — parse to extract |
| **DIRECT** | Commit email | `github_commits.author_email` | 0.98 | **Privacy bypass** — bypasses GitHub profile email hiding entirely |
| **DIRECT** | Profile email | `instagram_profiles.email`, `github_users.email` | 0.95 | When present |
| **DIRECT** | Profile phone | `instagram_profiles.phone` | 0.95 | Business/Creator accounts |
| **DIRECT** | Telegram numeric ID | `telegram_users.platform_user_id` | 0.99 | Permanent int ID — survives username changes |
| **FUZZY** | Username | all `*.username` fields | 0.70–0.90 | Normalize: lowercase, strip `._-` and trailing numbers |
| **FUZZY** | Real name | `strava_athletes.firstname+lastname`, `github_users.name`, `telegram_users.first_name+last_name` | 0.65–0.85 | Strava = highest quality (fitness identity, less pseudonymous) |
| **FUZZY** | Face embedding | `wa_face_embeddings.embedding vector(128)` | 0.80–0.95 | Already implemented; extend to all platforms |
| **FUZZY** | Profile photo SHA256 | `media_items.sha256` | 0.90 | Same photo on 2+ platforms = near-certain link |
| **INFERRED** | Bio NLP | `*.bio`, `whatsapp_users.about`, `github_users.bio` | 0.50–0.70 | Extract handles, occupation, location mentions |
| **INFERRED** | Location cluster | `instagram_posts.location_lat/lng`, `strava_gps_streams.latlng` | 0.60–0.80 | Home/work/gym inference from GPS clusters |
| **INFERRED** | Biometrics | `strava_athletes.weight/height/sex` | 0.50 | Narrows pool when combined with name/location |
| **INFERRED** | Structured data | `website_pages.structured_data` JSONB | 0.80 | JSON-LD `Person` schema: `email`, `sameAs` (explicit cross-platform links) |

### Confidence Scoring Formula

```
Score = 0 (base)
+ 40  if phone confirmed (telegram or whatsapp JID)
+ 35  if commit email present
+ 25  if profile email present
+ 30  if face embedding matched
+ 20  if profile photo SHA256 matched
+ 20  if username exact match 3+ platforms
+ 15  if real name consistent 2+ platforms
+ 10  if location cluster consistent
─────
Max theoretical: 195 pts → normalize to 0–100%
```

### Timeline Event Sources (all platforms)

| Priority | Table | Timestamp Column | Event Type |
|---|---|---|---|
| HIGH | `github_commits` | `date` | `CODE_COMMIT` — timezone in metadata, reveals local time |
| HIGH | `strava_activities` | `start_date` | `PHYSICAL_ACTIVITY` — has explicit `timezone` + `utc_offset` columns |
| HIGH | `instagram_posts` | `platform_created_at` | `CONTENT_PUBLISHED` |
| HIGH | `telegram_messages` | `platform_created_at` | `MESSAGE_SENT` |
| HIGH | `whatsapp_messages` | `timestamp` | `MESSAGE_SENT` |
| MED | `tiktok_posts` | `create_time` | `CONTENT_PUBLISHED` |
| MED | `youtube_videos` | `platform_published_at` | `VIDEO_PUBLISHED` |
| MED | `lemon8_posts` | `platform_created_at` | `CONTENT_PUBLISHED` |
| MED | `github_issues` | `created_at` / `closed_at` | `ISSUE_OPENED` / `ISSUE_CLOSED` |
| MED | `instagram_comments` | `platform_created_at` | `COMMENT_POSTED` — reveals activity even on private accounts |
| LOW | `strava_day_coverage` | `date` | `ACTIVITY_DAY` — gap detection: days with no data |

**Key:** `strava_activities.timezone` is the ONLY schema with explicit local timezone. Use it to calibrate all other UTC timestamps for behavioral analysis.

### Gap Signals — Free Tools Only (no API keys)

| Gap | Free Tool | How |
|---|---|---|
| Username on 300+ other platforms | **Sherlock** (local Python, no key) | `sherlock <username>` → list of URLs |
| Username with profile extraction | **Maigret** (local Python, no key) | `maigret <username>` → name, bio, location per platform |
| Email → platform presence | **Holehe** (local Python, no key) | Passive HTTP checks — no email sent |
| Deleted / historical content | **Wayback CDX API** (free REST, no key) | `https://web.archive.org/cdx/search/cdx?url=...` |
| Domain WHOIS registrant | **python-whois** (local library) | Free public WHOIS lookups |
| Reverse geocoding (GPS → address) | **Nominatim / OpenStreetMap** (free, 1 req/sec) | Reverse geocode Strava GPS to street addresses |
| Follower/following lists | Not stored despite spider_queue existing | Hard gap — no `instagram_followers` table |
| Instagram account creation date | Not exposed by API | Hard gap — no `platform_created_at` on profiles |

**Cut (require paid API):** HIBP, PimEyes, FaceCheck.ID, Hunter.io, Clearbit, Shodan, Numverify.  
**Face matching on public web:** Not possible free. But face matching **within your own collected data** is entirely local — dlib + pgvector, runs on your machine.

---

## 10. Noise Management — Entity Tiers

unifiedcollector casts a wide net → three tiers of data in the DB:

| Tier | Who | How they got in | Analyzer role |
|---|---|---|---|
| **Primary** | Explicit targets set in `collection_targets` | Intentional | Deep analysis, full profile |
| **Secondary** | Discovered through primaries (followers, group members, commenters) | Byproduct of spider/breadth | Surface when significant (3+ cross-platform interactions with primaries) |
| **Peripheral** | Appear once, never seen again | Incidental noise | Filterable, only show on demand |

Secondary entities ARE valuable — they form the social graph ("accomplices"). The analyzer just needs to surface them intelligently rather than treating them as noise.

---

## 11. External Tool Inspiration

### WorldView (Bilawal Sidhu)
4D, browser-based OSINT dashboard. The '4D' = spatial (lat/lng) + temporal + social network + content. Geo visualization with temporal playback on a 3D globe. This is the UX north star.

### Maltego — entity-transform model
Start with one entity (email/username/domain), run transforms, graph grows organically. AI assistant normalizes data (e.g. 'delete all entities not ending in @domain.com'). Visual link analysis. Key insight: **the graph IS the analysis.**

### Spiderfoot — modular data sources
200+ modules, passive vs active scanning. Passive = third-party APIs only (stealth). Active = direct target contact. Module types: network intel, social media, email/contact, dark web/breach.

### Trace Labs methodology
**'Advancing the timeline'** = finding events AFTER a specific date. Anomaly detection over absence. AI synthesis + human verification. Maps directly to our Timeline view.

### Open-Source Tools to Integrate (Phase 3)

| Tool | CLI pattern | What it gives us |
|---|---|---|
| **Sherlock** | `sherlock <username>` | URLs on 300+ platforms |
| **Maigret** | `maigret <username> --pdf` | Profile info per discovered platform |
| **Holehe** | `holehe <email>` | Which of 100+ services have this email registered |
| **theHarvester** | `theHarvester -d domain -b all` | Emails, names from public sources |
| **Wayback CDX API** | REST | Historical snapshots of profile pages |
| **HIBP API** | REST | Breach history for emails |

These are **enrichment modules** — triggered on-demand by analyst, not part of the core loop. Output feeds back into `collection_targets`.

---

## 12. Chosen Architecture

**Confirmed direction:** Identity Resolution + Timeline + Behavioral (Directions A+B+C combined). Personal OSINT terminal. Known targets primarily.

**UX model:** Bloomberg Terminal + WorldView — multi-panel dashboard, 5 views per entity.

**3 Modes:**
- ENTITY MODE — deep dive on one person
- INVESTIGATION MODE — correlate 2+ entities (shared locations, mutual contacts)
- DISCOVERY MODE — surface significant secondary entities from collected data

**5 Views per entity:**
1. **Identity Card** — unified profile: platform handles, real name candidates, contact info, face grid, confidence score + breakdown
2. **Timeline** — chronological cross-platform feed, filterable by source/type/date. Gap highlighting.
3. **Map** — Strava GPS routes + Instagram/Lemon8 post coordinates on real map
4. **Graph** — social graph: mentions, replies, follows, shared hashtags (Maltego-style)
5. **Behavior** — activity heatmap (hour×day), posting patterns, topic distribution, location frequency

---

## 13. Roadmap (revised 2026-06-08)

### Hardware Reality

- **CPU:** Intel i7-8565U (4C/8T, 1.8 GHz base) — 2018 ultrabook chip
- **RAM:** 16 GB
- **GPU:** Intel UHD 620 (integrated) — no CUDA, no GPU compute
- **Disk:** External HDD on Z: (190k+ files, ~57k YouTube, ~48k Telegram, ~23k TikTok, ~55k Lemon8)
- **Strava:** Cookie-only mode (API requires paid subscription). Zero GPS data.
- **Instagram:** 429-blocked. 1 file on disk. Effectively dead.

### Media Corpus (actual, 2026-06-08)

| Source | Total files | Images (jpg/png/webp) | Videos (mp4/webm) | JSON metadata |
|---|---|---|---|---|
| youtube | 57,941 | 28,036 | 935 | 28,970 |
| lemon8 | 55,022 | 28,032 | 0 | 28,042 (est: ~50/50 split) |
| telegram | 47,906 | 20,562 | 3,331 | 23,943 |
| tiktok | 23,233 | 8,412 | 3,207 | 11,611 |
| website | 38,884 | — | — | — |
| search | 4,131 | — | — | — |
| github | 3,349 | — | — | — |
| beeper | 181 | — | — | — |
| whatsapp | 17 | — | — | — |
| strava | 6 | 6 | 0 | 0 |
| instagram | 1 | — | — | — |
| **TOTAL** | **~190k** | **~85k images** | **~7.5k videos** | **~93k** |

### Phase 1 — v0.1: Identity + Timeline + Alerts (no ML, no location)

**Goal:** Shippable personal OSINT terminal. Open dashboard → see alerts → click entity → see unified profile + cross-platform timeline.

**Scope:**
- Separate `unifiedanalyzer` DB on same Postgres instance
- Read-only connection to `unifiedcollector` DB
- `entities`, `entity_platform_links`, `identity_signals`, `timeline_events`, `alerts`, `analysis_runs` tables
- Identity linker (deterministic signals only):
  - Username exact match (cross-platform, normalized)
  - WhatsApp JID → E.164 phone (runtime parse + validation)
  - GitHub commit `author_email` (post noreply filter)
  - Strava `firstname + lastname` (most reliable real name)
  - `media_items.sha256` profile photo dedup
  - Real name fuzzy match via `rapidfuzz` (token_sort_ratio ≥ 85, requires 2nd signal)
- Timeline builder: normalize all 10 platforms' timestamps into unified events table
- Alert engine: silence gap (dynamic + fixed fallback), new-activity-after-silence, profile change detection
- Scheduler: 60-min incremental + nightly full resolution + on-demand trigger
- API: `/entities`, `/entities/{id}`, `/entities/{id}/timeline`, `/alerts`, `/runs`, `/health`
- React frontend: entity list, entity card (identity signals + platform links), timeline view, alerts home screen
- Docker Compose: FastAPI + React frontend. NO Postgres (connects to collector's Postgres).

**Explicitly NOT in Phase 1:**
- Face recognition
- Bio NLP / sentence-transformers
- Map view (zero location data)
- Behavioral heatmaps (no reliable timezone source)
- Graph analytics
- Enrichment tools (Sherlock, Maigret, Holehe)

**Estimated effort:** 2-3 weeks to shippable v0.1.

---

### Phase 2 — v0.2: Face Recognition + Behavioral Profiling + Text Intelligence

**Goal:** ML layer on top of Phase 1. Identify recurring faces across collected media. Extract behavioral patterns from posting timestamps. Extract text from PDFs/images.

**Scope:**
- **Face recognition pipeline (dlib, CPU-only):**
  - Frame extraction from videos: sample 1 frame per 5 sec via OpenCV, pHash dedup (~60% reduction)
  - Face detection (dlib HOG detector, ~0.3 sec/frame)
  - 128-dim face embedding extraction (dlib ResNet)
  - Face matching via pgvector cosine similarity (threshold 0.6)
  - Initial corpus: ~85k images + ~7.5k videos (~18k unique frames after dedup) = **~8 hours CPU for initial scan**
  - Incremental: process only new `media_items` rows since last run
  - Tables: `face_identities`, `face_embeddings`, `face_scan_progress`
  - Configurable: `FACE_FRAMES_PER_SECOND=0.2` (1 per 5 sec), `FACE_DETECTOR=hog|cnn`, `FACE_MATCH_THRESHOLD=0.6`
- **Behavioral profiling:**
  - Activity heatmap (hour × day-of-week) from posting timestamps
  - Timezone inference: fallback "auto" mode (most common activity hour distribution) — no Strava timezone available
  - Posting frequency computation (90-day rolling window)
  - Sleep/wake inference (when enough data: 100+ events)
  - Mark profiles with insufficient data as `below_threshold` in UI
- **Text extraction:**
  - PDF text via PyMuPDF (website + github sources)
  - Image OCR via EasyOCR (free, CPU, no API key) for screenshots, infographics
- **Bio NLP similarity (sentence-transformers):**
  - `all-MiniLM-L6-v2` model (~80 MB, runs on CPU)
  - Compare bios across platforms for identity linking
  - Adds a new signal type to identity resolution
- API additions: `/entities/{id}/behavior`, `/entities/{id}/faces`
- Frontend additions: behavioral heatmap component, face gallery per entity

**Estimated effort:** 3-4 weeks. Face scan initial run: 1 weekend of background processing.

---

### Phase 3 — v0.3: Graph Analytics + Enrichment Tools

**Goal:** Social graph visualization. External tool enrichment for gap-filling.

**Scope:**
- **Graph analytics (networkx):**
  - Build social graph from: Telegram group co-membership, reply threads (Telegram/WhatsApp), Instagram/TikTok comments + mentions, YouTube comment threads, GitHub contribution overlap
  - Centrality scores, community detection, influence ranking
  - Tables: `entity_relationships` (weight, cross_platform flag, last_seen_at)
  - Secondary entity promotion logic (weight ≥ 2 AND cross-platform)
- **Enrichment modules (on-demand, not scheduled):**
  - Sherlock: username → 300+ platform presence check. Results → discovery view at confidence=0.30.
  - Maigret: username → profile info extraction (name, bio, location per platform). Slower but richer than Sherlock.
  - Holehe: email → platform registration check. Max 1x/month per email.
  - Wayback CDX API: historical snapshots of profile pages (best for GitHub, YouTube; useless for Instagram/Telegram/Strava).
- **Map view (conditional):**
  - Only if a free geo source becomes available (e.g., Lemon8 location_name geocoding, or if Strava re-opens free API)
  - Reverse geocoding via Nominatim (free, 1 req/sec) for any coordinates that exist
  - If zero geo data: map view remains disabled
- API additions: `/entities/{id}/graph`, `/discovery`, enrichment trigger endpoints
- Frontend additions: graph visualization (force-directed or Maltego-style), discovery view

**Estimated effort:** 4-6 weeks.

---

### Phase 4 — v1.0: Investigation Mode + Polish

**Goal:** Multi-entity correlation. Production polish.

**Scope:**
- **Investigation mode:** Correlate 2+ entities side-by-side (shared locations, mutual contacts, timeline overlap, co-occurrence in groups)
- **Discovery mode:** Surface significant secondary entities automatically (promoted from peripheral tier)
- **Topic modeling:** YouTube transcripts + GitHub READMEs → topic clusters via embedding + clustering
- **Content similarity:** Cross-platform content reuse detection (same text posted on multiple platforms)
- **Link retraction:** `retracted_at` + `retraction_reason` on entity_platform_links; nightly run respects retractions
- **Schema validation on startup:** Verify expected collector tables/columns exist before running
- **Performance hardening:** Timeline partitioning (monthly), incremental behavioral updates, `ana_analysis_runs` run-lock to prevent concurrent runs
- **WebSocket live alerts** via `/ws/alerts`
- Frontend polish: responsive design, search, filters, entity comparison view

**Estimated effort:** 4-6 weeks.
## 14. Final Locked Decisions (revised 2026-06-08)

| # | Decision | Choice |
|---|---|---|
| 1 | Identity linking strategy | Option A — conservative. Confidence ≥ 0.85 + min 2 independent signals. |
| 2 | Home screen | Alerts view — what changed since last visit. |
| 3 | Secondary entity promotion | weight ≥ 2 AND (cross_platform=TRUE OR seen with 2+ different primaries). |
| 4 | Face recognition | Phase 2 only. Analyzer-owned. `wa_face_*` tables in collector are a design artifact — analyzer will own all face processing eventually. |
| 5 | Face needed at all? | Not for Phase 1 (known targets don't need face to resolve). Phase 2 value: identifying recurring unknowns in collected media. |
| 6 | Paid APIs | Zero. Sherlock, Maigret, Holehe (local), Wayback CDX, Nominatim, python-whois only. |
| 7 | Repo | Separate (`unifiedanalyzer`). **Separate database** (`unifiedanalyzer` DB on same Postgres instance). Reads `unifiedcollector` DB via read-only connection. Does NOT share a DB or use `ana_*` prefix in collector DB. |
| 8 | UI | FastAPI + React. Frontend served as static from same process. |
| 9 | Run mode | Scheduled: **60-min** incremental + nightly full resolution + weekly enrichment + on-demand trigger per entity. (Changed from 30-min — 1000 targets is too heavy for 30-min cycles on i7-8565U.) |
| 10 | Entity creation | Automatic — scheduler watches `collection_targets` for new rows. |
| 11 | theHarvester / HIBP | Removed — HIBP is paid, theHarvester adds little beyond Maigret for this use case. |
| 12 | Day-1 use case | **A + B combined**: Alerts ("what happened while I slept") + Entity deep-dive ("tell me everything about person X"). Timeline + Identity Card + Alerts. |
| 13 | Target count | ~1000 primary targets. Architecture must handle batch-oriented processing, not one-at-a-time. Identity resolution in v0.1 matches primary targets only (1000×1000). Secondary/peripheral entity matching deferred to Phase 3. |
| 14 | Strava OAuth | **NOT AVAILABLE** — Strava API requires paid subscription. Cookie-only mode. Zero GPS data. Map view deferred to Phase 3+. Timezone inference uses fallback "auto" mode (posting hour distribution). |
| 15 | Location data | **Zero.** Strava GPS = paid API only. Instagram lat/lng = hardcoded `download_geotags=False`. Lemon8 = text `location_name` only (no coordinates). Map view deferred. |
| 16 | Analyzer filesystem access | Read-only on `Z:/unifiedcollector/media`. Can create temp/cache files in its own directories (face index, embeddings, dashboard assets). |
| 17 | Collector modifications | Analyzer never modifies collector tables at runtime. If a missing column blocks the analyzer, fix it in collector repo separately. |
| 18 | Legal/ethical guardrails | None in code. No PDPA/CMA checks. |
| 19 | Greenfield codebase | Zero code reuse from collector. Own asyncpg pool, own models, own pipeline. |
| 20 | Data source priority | DB-first. Analyzer discovers files via `media_items` rows, reads content from Z:/ as needed. Does not walk filesystem independently. |

---

## 15. Data Model (locked)

8 tables, all `ana_` prefixed:

| Table | Purpose |
|---|---|
| `ana_entities` | One row per resolved person. tier, canonical_name, confidence_score, last_seen_at, primary_timezone. |
| `ana_entity_platform_links` | entity ↔ platform profile. confidence, link_method, is_confirmed. UNIQUE(source, platform_id). |
| `ana_identity_signals` | Evidence ledger. Every signal that drove a link decision, with source table/column/row reference. |
| `ana_timeline_events` | Unified chronological feed. All 10 platforms normalized. INDEX(entity_id, occurred_at DESC). |
| `ana_behavioral_profiles` | Aggregated stats. posting_hour_dist, posting_dow_dist, frequent_locations, inferred_home. |
| `ana_entity_relationships` | Social graph edges. weight, cross_platform flag. Nullable entity IDs (pre-resolution). |
| `ana_alerts` | Home screen feed. alert_type, severity, is_read, detected_at. |
| `ana_analysis_runs` | Scheduler audit log. run_type, status, counts, timestamps. |

---

## 16. Repo Structure (locked)

```
unifiedanalyzer/
├── src/
│   ├── main.py                      # CLI
│   ├── pipeline/                    # entity_resolver, timeline_builder,
│   │                                # behavior_profiler, relationship_mapper, alert_engine
│   ├── enrichment/                  # sherlock, maigret, holehe, wayback, nominatim
│   ├── db/                          # asyncpg connection + analyzer.sql schema
│   ├── scheduler/                   # orchestrates run cadences
│   └── api/                         # FastAPI + routes + websocket + frontend/
├── docker/docker-compose.yml        # FastAPI + frontend only. NO postgres.
├── .env.example
└── requirements.txt
```

---

## 17. API Contract (locked)

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/entities` | List all entities |
| GET | `/api/entities/{id}` | Full entity card |
| GET | `/api/entities/{id}/timeline` | Paginated events (?from, ?to, ?source, ?type) |
| GET | `/api/entities/{id}/behavior` | Behavioral profile |
| GET | `/api/entities/{id}/graph` | {nodes[], edges[]} |
| GET | `/api/entities/{id}/map` | Geo events + Strava route polylines |
| GET | `/api/alerts` | Paginated alerts (?unread_only, ?entity_id) |
| POST | `/api/alerts/{id}/read` | Mark one read |
| POST | `/api/alerts/read-all` | Mark all read |
| GET | `/api/discovery` | Secondary entities pending awareness |
| GET | `/api/runs` | Analysis run history |
| POST | `/api/runs/trigger` | Manual incremental run |
| GET | `/api/health` | DB status + last run times |
| WS | `/ws/alerts` | Live alert push |

---

## 18. Alert Rules (locked)

### Silence Gap

**Strategy: Dynamic by default, fixed fallback.**

Dynamic = uses each target's own average posting frequency as the baseline. If they post every 2 days on average, silence fires after 5 days (2 × 2.5 multiplier). More useful than a blanket threshold because it respects each person's actual behaviour.

Fixed fallback = kicks in when less than 14 days of history exists (new targets). Default: 7 days.

| Knob | Default | Meaning |
|---|---|---|
| `SILENCE_GAP_DYNAMIC` | `true` | Use target's own baseline |
| `SILENCE_GAP_MIN_HISTORY_DAYS` | `14` | Days of data needed before dynamic mode activates |
| `SILENCE_GAP_FIXED_DAYS` | `7` | Fallback threshold when history insufficient |
| `SILENCE_GAP_DYNAMIC_MULTIPLIER` | `2.5` | Multiply avg post frequency by this to get threshold |
| `SILENCE_GAP_MIN_DAYS` | `3` | Floor — never fires before 3 days regardless of baseline |
| `SILENCE_GAP_MAX_DAYS` | `30` | Ceiling — always fires after 30 days regardless of baseline |

**Example:** Target posts every 2 days on average → silence fires after 5 days (2 × 2.5). Target posts every 14 days → would fire after 35 days, but ceiling clamps it to 30.

### Location Anomaly

**Strategy: distance from nearest known location cluster.**

"Home" is not necessarily where anomalies are measured from — the target may have multiple regular locations (home, work, gym, parents). Anomaly = activity detected more than X km from the nearest of ANY known significant cluster.

Default: **100km**. Catches international travel and long-distance movement without false-positiving on normal daily commutes or weekend trips to a nearby city.

| Knob | Default | Meaning |
|---|---|---|
| `LOCATION_ANOMALY_KM` | `100` | Distance from nearest known cluster to trigger alert |
| `LOCATION_ANOMALY_MIN_CLUSTER_EVENTS` | `5` | Min events to establish a cluster before anomaly detection activates |

Anomaly detection is silenced for a target until they have at least 5 geotagged events — not enough data to know what "normal" looks like.

### New Activity After Silence

**Only fires when the preceding gap was ≥ the silence threshold.**

Rationale: if a target posts daily, every post is not interesting. The alert is specifically "they went quiet, and now they're back." Only meaningful content types trigger it (posts, messages, physical activities) — not low-intent actions like a single comment.

| Knob | Default | Meaning |
|---|---|---|
| `NEW_ACTIVITY_AFTER_SILENCE_ENABLED` | `true` | Enable the "back online" alert |
| `NEW_ACTIVITY_MIN_EVENT_TYPES` | `post,message,activity` | Which event types count as "real" activity |

---

## 19. Full `.env.example` — All Knobs

```env
# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
# Analyzer's own database (separate from collector).
ANALYZER_DATABASE_URL=postgres://analyzer:analyzer@localhost:5432/unifiedanalyzer

# Read-only connection to collector's database.
COLLECTOR_DATABASE_URL=postgres://collector:collector@localhost:5432/unifiedcollector

# asyncpg connection pool size
DB_MAX_POOL_SIZE=10


# ─────────────────────────────────────────────
# SCHEDULER CADENCES
# ─────────────────────────────────────────────
# How often to check for new collector data and update timeline/alerts.
# 60 min chosen for 1000-target scale on i7-8565U. Smaller = fresher but heavier.
INCREMENTAL_RUN_INTERVAL_MINUTES=60

# UTC hour (0-23) for nightly full identity re-resolution.
# Re-runs all 3 identity stages against full dataset. Slow but thorough.
FULL_RESOLUTION_HOUR=3

# UTC hour for daily silence/gap scan.
GAP_DETECTION_HOUR=6

# Day of week for weekly external enrichment run (0=Mon … 6=Sun).
# Only applies if enrichment tools are enabled.
ENRICHMENT_DAY_OF_WEEK=0


# ─────────────────────────────────────────────
# IDENTITY RESOLUTION
# ─────────────────────────────────────────────
# Minimum confidence score (0.0–1.0) to mark a platform link as confirmed.
# Below this threshold, links are stored as candidates only (dotted in UI).
IDENTITY_CONFIDENCE_THRESHOLD=0.85

# Minimum number of independent signals required alongside the confidence score.
# Prevents a single very-high-confidence signal from auto-confirming on its own.
IDENTITY_MIN_SIGNALS=2

# Characters stripped from usernames before exact-match comparison.
USERNAME_NORMALIZE_STRIP_CHARS=._-

# Whether to strip trailing digits from usernames before comparison.
# e.g. bryanseah234 → bryanseah
USERNAME_NORMALIZE_STRIP_TRAILING_DIGITS=true

# Minimum rapidfuzz token_sort_ratio score (0–100) for a real name to be
# considered a fuzzy match. 85 = 'Bryan Seah' matches 'Seah Bryan' but not
# 'Brian Shaw'.
NAME_FUZZY_MIN_SCORE=85


# ─────────────────────────────────────────────
# SECONDARY ENTITY PROMOTION
# ─────────────────────────────────────────────
# Minimum interaction count (weight in ana_entity_relationships) before a
# secondary entity is auto-promoted from peripheral to secondary tier.
SECONDARY_PROMOTION_MIN_WEIGHT=2

# Whether promotion requires the interactions to span 2+ platforms OR
# involve 2+ different primary targets. Prevents a person who comments twice
# on one post from being promoted.
SECONDARY_PROMOTION_REQUIRE_CROSS=true


# ─────────────────────────────────────────────
# ALERT RULES
# ─────────────────────────────────────────────

# SILENCE GAP
# Use each target's own posting frequency as the baseline threshold.
# Falls back to SILENCE_GAP_FIXED_DAYS when insufficient history.
SILENCE_GAP_DYNAMIC=true

# Minimum days of event history before dynamic mode activates.
# Below this, SILENCE_GAP_FIXED_DAYS is used instead.
SILENCE_GAP_MIN_HISTORY_DAYS=14

# Fixed fallback threshold (days) when history is insufficient.
SILENCE_GAP_FIXED_DAYS=7

# Dynamic threshold = avg_post_interval × this multiplier.
# e.g. posts every 2 days → silence fires after 5 days (2 × 2.5).
SILENCE_GAP_DYNAMIC_MULTIPLIER=2.5

# Floor: never fires before this many days regardless of baseline.
SILENCE_GAP_MIN_DAYS=3

# Ceiling: always fires after this many days regardless of baseline.
SILENCE_GAP_MAX_DAYS=30

# NEW ACTIVITY AFTER SILENCE
# Fire an alert when a target becomes active again after a silence gap.
# Only fires if the preceding gap was >= the silence threshold.
NEW_ACTIVITY_AFTER_SILENCE_ENABLED=true

# Comma-separated event types that count as 'real' activity for this alert.
# Low-intent events like single comments are excluded by default.
NEW_ACTIVITY_EVENT_TYPES=CONTENT_PUBLISHED,MESSAGE_SENT,PHYSICAL_ACTIVITY,CODE_COMMIT,VIDEO_PUBLISHED

# LOCATION ANOMALY
# Distance in km from the nearest known location cluster to trigger an alert.
# 100km catches international travel without false-positiving on daily movement.
LOCATION_ANOMALY_ENABLED=true
LOCATION_ANOMALY_KM=100

# Minimum geotagged events needed to establish location clusters before
# anomaly detection activates for a target. Not enough data = no alert.
LOCATION_ANOMALY_MIN_CLUSTER_EVENTS=5

# OTHER ALERT TOGGLES
# New high-confidence identity signal found (e.g. email match discovered).
IDENTITY_SIGNAL_ALERT_ENABLED=true

# A secondary entity was auto-promoted.
SECONDARY_PROMOTION_ALERT_ENABLED=true

# Target's bio, display name, or profile photo changed.
PROFILE_CHANGE_ALERT_ENABLED=true

# Target appears linked to a platform not previously seen.
NEW_PLATFORM_ALERT_ENABLED=true


# ─────────────────────────────────────────────
# GEO / MAP
# ─────────────────────────────────────────────
# Enable reverse geocoding of GPS coordinates to human-readable addresses.
# Uses Nominatim (OpenStreetMap). Free, no API key.
NOMINATIM_ENABLED=true

# Seconds between Nominatim requests. OSM usage policy requires max 1 req/sec.
# 1.1 gives a safe margin.
NOMINATIM_RATE_LIMIT_SECONDS=1.1

# Nominatim endpoint. Default uses public OSM server.
# Replace with self-hosted instance for higher throughput.
NOMINATIM_BASE_URL=https://nominatim.openstreetmap.org

# Radius in meters to group GPS points into a location cluster.
# 500m = roughly the size of a neighbourhood block.
HOME_CLUSTER_RADIUS_M=500

# Minimum events in a cluster to consider it 'significant' for home/work
# inference and anomaly detection baseline.
HOME_CLUSTER_MIN_EVENTS=5


# ─────────────────────────────────────────────
# BEHAVIOURAL ANALYSIS
# ─────────────────────────────────────────────
# Where to source the inferred timezone.
# 'strava' = use strava_activities.timezone (most reliable, explicit field).
# 'auto' = infer from most common activity hour distribution.
BEHAVIOR_TIMEZONE_SOURCE=strava

# Minimum events needed to generate a meaningful activity heatmap.
BEHAVIOR_HEATMAP_MIN_EVENTS=10

# Rolling window (days) for computing average posting frequency.
# 90 days = captures recent behaviour without being thrown off by old patterns.
BEHAVIOR_FREQUENCY_WINDOW_DAYS=90


# ─────────────────────────────────────────────
# API / UI
# ─────────────────────────────────────────────
# Port for the FastAPI server. Use 8001 to avoid collision with
# unifiedcollector's dashboard (typically 8000).
API_PORT=8001
API_HOST=0.0.0.0

# Secret for JWT auth on the dashboard. Generate with: openssl rand -hex 32
JWT_SECRET=

# How long a login session stays valid.
JWT_EXPIRY_HOURS=24


# ─────────────────────────────────────────────
# ENRICHMENT TOOLS (Phase 3 — disabled by default)
# ─────────────────────────────────────────────
# Master switch. Set to true to enable any of the tools below.
ENRICHMENT_ENABLED=false

# Sherlock: checks 300+ platforms for a given username.
# Results surface in Discovery view as potential new collection targets.
SHERLOCK_ENABLED=false
SHERLOCK_PATH=sherlock
SHERLOCK_TIMEOUT_SECONDS=300

# Maigret: enhanced username search with profile info extraction.
# Slower than Sherlock but returns name, bio, location per discovered profile.
MAIGRET_ENABLED=false
MAIGRET_PATH=maigret
MAIGRET_TIMEOUT_SECONDS=600

# Holehe: passive check of which platforms have an account for a given email.
# No email is sent — purely HTTP probing.
HOLEHE_ENABLED=false
HOLEHE_PATH=holehe

# Wayback Machine CDX API: fetches historical snapshots of profile pages.
# Useful for recovering deleted bios, old usernames, removed posts.
WAYBACK_ENABLED=false
WAYBACK_RATE_LIMIT_SECONDS=2.0


# ─────────────────────────────────────────────
# PHASE 2 FLAGS (disabled — do not enable until Phase 2 build)
# ─────────────────────────────────────────────
# Face recognition across all collected media.
# Requires dlib models downloaded to DLIB_MODELS_PATH.
FACE_RECOGNITION_ENABLED=false
DLIB_MODELS_PATH=models/dlib/

# L2 distance threshold for face matching (0.0–1.0).
# Lower = stricter match. 0.6 is the standard dlib recommendation.
FACE_MATCH_DISTANCE_THRESHOLD=0.6

# Bio NLP similarity via sentence-transformers (local, no API key).
# Model is ~80MB and runs on CPU.
BIO_NLP_ENABLED=false
BIO_NLP_MODEL=all-MiniLM-L6-v2
BIO_NLP_SIMILARITY_THRESHOLD=0.85

# Topic extraction from text corpora (YouTube transcripts, GitHub READMEs,
# Instagram captions, etc.).
TOPIC_MODELING_ENABLED=false

# Social graph analytics via networkx.
# Computes centrality scores, community clusters, influence ranking.
GRAPH_ANALYTICS_ENABLED=false
```

---

## 21. Limitations, Shortsightedness & Known Failure Modes

> **Source:** Code audit of 5 collector modules (instagram.py, strava.py, github.py, telegram.py, whatsapp.py) + architectural analysis.
> This section is mandatory reading before building any pipeline module.

---

### SECTION A — Data Reality Gaps
### (What the schemas promise vs. what the collector actually stores)

These are not theoretical concerns. They were verified by reading the actual upsert logic in each collector.

#### A1. Instagram

| Assumed signal | Reality |
|---|---|
| `instagram_profiles.email` | **NEVER populated.** The Instagram private API does not return email. Always NULL. Remove from identity pipeline entirely. |
| `instagram_profiles.phone` | **NEVER populated.** Same reason. Always NULL. |
| `instagram_posts.location_lat/lng` | **NEVER populated.** `download_geotags=False` is hardcoded in the instaloader init. Zero location data from Instagram. Remove all Instagram geo logic from the map view. |
| Posts exist for private accounts | **False.** `is_private=True` profiles have a non-zero `posts_count` but zero rows in `instagram_posts`. The count field is from the public profile header, not from actual collection. A join will return empty. |
| Follower/following graph exists | **Not implemented.** `_spider_followers` is a stub. The follower spidering code exists in the queue logic but the collection itself is not done. No social graph from Instagram. |

**Net impact:** Instagram contributes `username`, `full_name` (often blank), `bio` (often blank), `profile_pic_url`, engagement counts, and post captions/timestamps only. No contact info, no location, no social graph.

---

#### A2. Strava

| Assumed signal | Reality |
|---|---|
| GPS streams exist for all activities | **Only collected via OAuth (API mode).** Cookie-only mode (`STRAVA_SESSION_COOKIE` without OAuth tokens) never calls `_collect_gps_streams`. If the collector was configured without OAuth, `strava_gps_streams` is completely empty. Always check `SELECT COUNT(*) FROM strava_gps_streams` before assuming GPS data exists. |
| GPS represents true home/work location | **Strava applies privacy zones silently.** For activities starting/ending near a configured privacy zone (200m–2km radius around home/work), Strava's API removes the first and last N seconds of the GPS trace before returning it. No flag is set in the response or stored in the DB. The route will appear to start/end at a random point near (but not at) the sensitive location. This cannot be detected from the data alone. |
| `strava_activities.start_latlng` exists | **Not a queryable column.** The start/end latlng from the API response is only preserved in `metadata` JSONB, not in a dedicated column. Geospatial queries must extract from JSONB. |
| Cookie-mode returns full activity data | **Severely degraded.** Web-scrape mode produces NULL for: `city`, `state`, `country`, `sex`, `profile` photo, `follower_count`. `start_date` parsing from human-readable strings can silently fail to NULL. Activities themselves are not collected in cookie-only mode at all — only the athlete profile. |
| `athlete_id` on activities is always resolved | **Not guaranteed.** `_collect_feed` collects other athletes' activities from the social feed. These activities reference athletes who may not have their own row in `strava_athletes` yet, causing `athlete_uuid` to be NULL. |

**Mitigation:** Add an `is_gps_available` boolean to `ana_behavioral_profiles`. Disable all GPS-dependent analysis (map view, location clustering, anomaly detection) if false.

---

#### A3. GitHub

| Assumed signal | Reality |
|---|---|
| `github_users.email` is a real email | **Only returned if user set email to public.** Majority of users have NULL here. |
| `github_commits.author_email` bypasses privacy | **Partially true, but broken by noreply masking.** GitHub's "Keep my email private" feature replaces real emails with `{id}+{username}@users.noreply.github.com`. The collector stores these verbatim with no flag. Running email-based identity matching against noreply addresses will always fail and produce false negatives. **Filter: `WHERE author_email NOT LIKE '%@users.noreply.github.com'`** |
| `author_login` and `author_email` identify the same person | **Can diverge completely.** `author_login` is GitHub's account match; `author_email` is the raw git metadata. For unlinked/imported/deleted accounts, `author_login` is NULL while `author_email` still has a value. Never join them as if they're the same identity anchor. |
| Commits are linked to repos | **No `repo_id` column in `github_commits`.** Commits are deduplicated by SHA only. A commit that appears in multiple forks only has one row, linked to whichever repo collected it first. The repo context is lost. |
| `github_users` fields stay current | **Stale after first insert.** `ON CONFLICT DO UPDATE` only refreshes `login`, `name`, `bio`, `public_repos_count`. Fields like `email`, `company`, `blog`, `location` are never updated after initial collection. |

**Net impact:** GitHub is still one of the best identity sources (commit emails pre-noreply filter, real names via Strava-GitHub cross-match), but the noreply filter is load-bearing — it must be applied before any email matching.

---

#### A4. Telegram

| Assumed signal | Reality |
|---|---|
| `telegram_users.phone` is populated | **NEVER collected.** Telegram MTProto only exposes a contact's phone if they are in your contacts. Collector accounts do not add targets as contacts. The `_upsert_sender` does not even include `phone` in its INSERT. This was a Tier-1 identity signal in our design — it does not exist. |
| `telegram_users.username` is a stable anchor | **Optional field, frequently NULL.** Many Telegram users have no @username. The only stable identifier is the numeric `platform_user_id`. |
| Deleted accounts produce NULL rows | **Worse: orphaned messages.** `get_entity()` on a deleted account raises an exception, which is caught and returns None. Messages from deleted-account senders are stored with `sender_id = NULL` permanently. |
| DMs are distinguishable from group chats | **Not distinguishable from type column.** Both private 1-on-1 chats and group chats are stored as `type = 'group'`. |
| Channel messages are attributed to a person | **Channel posts have `sender_id = NULL`.** Channels post as the channel entity, not as a user. All channel messages are unattributable. |

**Net impact:** Telegram phone number as a Tier-1 identity signal is eliminated. The only Telegram identity signals are: numeric user ID (stable, but platform-only), username (when present, sparse), and first/last name (unreliable). Telegram contributes timeline events and social graph edges (reply threads, group memberships) but minimal identity value.

---

#### A5. WhatsApp

| Assumed signal | Reality |
|---|---|
| WhatsApp JID → E.164 phone is reliable | **Partially.** Standard individual JIDs (`{phone}@s.whatsapp.net`) do yield a phone via split. BUT: business accounts may use `@c.us` suffix (legacy) or LID format (`{opaque_id}@lid`), which is WhatsApp's privacy-preserving linked device ID — splitting on `@` gives a non-phone opaque ID. Status broadcast JIDs (`status@broadcast`) produce `"status"` as the extracted number. Always validate: extracted value must be 7–15 digits and pass basic E.164 format check. |
| `phone_number` is stored in `whatsapp_users` | **Dead code.** `payload["phone_number"]` is computed correctly but never appears in the INSERT statement. The column does not exist. Phone extraction from JID must be done at query time from `platform_user_id`, not from a stored column. |
| Group messages always have sender attribution | **Not guaranteed.** `sender_id` is NULL when the bridge implementation omits the `participant` field in group message events. This is bridge-dependent — some bridge configs produce this routinely. |
| `whatsapp_users.name` has the contact name | **Often empty string, not NULL.** `name` defaults to `""` when no verified business name or `notify` field is present. Empty string behaves differently from NULL in queries — `WHERE name IS NOT NULL` will still return empty-string rows. Filter: `WHERE name != ''`. |
| `is_business` flag is stored | **Dead code.** Computed in payload but not in INSERT. Lost. |

**Net impact:** WhatsApp JID is still a valid phone-extraction source but requires runtime parsing with format validation. Business/LID accounts are unreliable. WhatsApp contributes message timeline and the wa_face_* tables (when populated).

---

### SECTION B — Identity Resolution Failure Modes

#### B1. Signal Independence is Not Guaranteed

The confidence scoring formula requires **2 independent signals**. But signals can be derived from the same underlying fact:

- A user sets the same username everywhere (Instagram, GitHub, TikTok, Lemon8, YouTube). This counts as 5 signals but it's 1 decision — they chose this username. It's still strong evidence, but it's not 5 independent observations.
- Username appearing in both `github_users.login` AND `github_commits.author_login` would count twice but is literally the same account.
- **Mitigation:** When computing signal independence, deduplicate by `(signal_type, underlying_source_table)`. Two username matches from two different platforms = independent. Two username matches from two rows in the same table = not independent.

#### B2. Username Recycling

Platforms allow username changes and eventual recycling. `@bryanseah234` on Instagram in 2022 may not be the same person as `@bryanseah234` on Instagram in 2026 if the account was deleted and re-registered. The collector stores whatever it found at collection time. The analyzer has no way to know if a username was recycled unless it detects a dramatic content/behavioral shift.

#### B3. Common Name Collision

Real name fuzzy matching (e.g., "Bryan Seah" on Strava + "Bryan Seah" on GitHub) fails when the name is common. The confidence must be weighted by name uniqueness — a name match for "John Smith" carries much less weight than a match for "Thorvaldsen Mikkelborg". **The current confidence formula treats all name matches equally regardless of name rarity.** A name frequency lookup (or at minimum a length heuristic) should weight rare names higher.

#### B4. Shared and Corporate Accounts

A GitHub org account, a family Strava account, or a corporate Instagram profile may be operated by multiple people. The analyzer will attempt to link these to a single entity when they should be flagged as multi-operator. No mechanism exists in the current design to detect or flag shared accounts.

#### B5. Cascading Wrong Links (The Critical Failure Mode)

If Entity A and Entity B are incorrectly linked into one `ana_entities` record, the damage is significant:
- Their timelines merge — behavioral profiles become meaningless noise
- Their social graphs merge — false relationships are created
- Location clusters merge — location intelligence is corrupted
- There is **no automated retraction mechanism** in the current design

The conservative linking strategy (Option A, confidence ≥ 0.85 + 2 signals) mitigates this but does not eliminate it. **The `ana_identity_signals` ledger is critical for forensics** — when a wrong link is suspected, the evidence trail allows manual diagnosis.

**Recommended addition:** an `ana_entity_link_retraction_log` table that records when `is_confirmed` is manually set back to FALSE, what signals were revoked, and why. Even for a personal tool, this audit trail is valuable when an identity conclusion turns out to be wrong.

#### B6. Intentional Deception

A sophisticated target who knows they are being tracked can deliberately use the same username across all platforms. This inflates the confidence score without adding independent evidence. The system will confidently confirm a link that the target manufactured. Conversely, a target who uses different usernames everywhere will have low confidence scores even when the same person. The system is not adversarially robust — it is designed for passive correlation of public data, not for tracking evasion-aware subjects.

---

### SECTION C — Behavioral Profiling Limitations

#### C1. Posting Time ≠ Activity Time

The single most important limitation of behavioral profiling from social media timestamps:

> **Scheduled posts completely decouple posting time from actual activity.**

Instagram, TikTok, YouTube, and Lemon8 all support native scheduling or third-party tools (Buffer, Later, Hootsuite). A post timestamped at 9:00 AM may have been composed at 11 PM and scheduled. The posting-schedule heatmap reflects **when they want to be seen**, not when they are actually active. Only platforms where scheduling is technically impossible or uncommon (WhatsApp messages, Telegram messages, Strava activities, GitHub commits) provide reliable behavioral timestamps.

#### C2. Sparse Data Makes Profiling Unreliable

Minimum events before patterns become statistically meaningful:

| Analysis | Minimum events | Notes |
|---|---|---|
| Posting hour heatmap | ~50 events | Below this, any 1-2 events skew the entire hour's weight |
| Sleep/wake inference | ~100 events | Requires enough samples to distinguish night silence from irregular schedule |
| Average posting frequency | ~30 events across 90 days | Less than this and a single busy week distorts the average |
| Location home cluster | ~10 geotagged events | Below 5, cluster centroid is unreliable; 5–9 is minimum viable |
| Behavioral change detection | ~50+ events per period | Need enough baseline to define "normal" before detecting deviation |

**Implication:** For new targets or infrequent users, most behavioral analysis will be below threshold and should be suppressed in the UI rather than displayed with misleading visualizations.

#### C3. Timezone Inference is Fragile

The plan is to use `strava_activities.timezone` as the primary timezone source. Limitations:
- **Strava timezone is the device's timezone at time of activity recording.** If the user travels, the timezone reflects their travel location, not their home timezone. A week of holiday in Tokyo will shift the inferred timezone to JST.
- **`BEHAVIOR_TIMEZONE_SOURCE=strava` fails completely** if the collector is in cookie-only mode (no activities collected, as per the Strava data reality above).
- **Fallback `auto` mode** (inferring from most common activity hour) assumes the person is most active during waking hours — fails for night-shift workers, insomniacs, and people in multiple timezones.

#### C4. Strava Privacy Zones Corrupt Location Intelligence

This deserves its own entry. Strava privacy zones mean:
- Home location inference from GPS clusters will produce a cluster centroid that is systematically offset from the real home by 200m–2km
- The cluster still exists and is still useful (it narrows the area), but the analyzer cannot know the offset direction
- Reverse geocoding (Nominatim) will return a nearby street/building, not the actual home
- **Location anomaly detection is unaffected** — anomalies are based on deviation from the cluster, not the cluster's absolute accuracy

---

### SECTION D — Tool-Specific Limitations

#### D1. Sherlock — Username Search

- **High false positive rate for common usernames** on sites that return HTTP 200 for any username (e.g., WordPress.com, Gravatar, About.me). The site just shows a profile page regardless of whether the account exists. Sherlock's detection heuristics catch many but not all of these.
- **Result is URL, not profile data.** Sherlock confirms existence; it does not return the profile's name, bio, or photo. Maigret extracts that but is 10× slower.
- **Sites go offline / change response patterns.** Sherlock's site list is community-maintained and frequently has stale entries. A result of "not found" may mean the account doesn't exist OR the site changed its response format.
- **Cannot distinguish an account that is the target vs. a different person with the same username.** A Sherlock result for `bryanseah234` on a random platform requires manual verification.
- **Mitigation:** Use Sherlock results as suggestions to investigate, not as confirmed identity links. Never auto-add Sherlock results to `ana_entity_platform_links` at high confidence. Feed into `ana_discovery` with `confidence=0.30`.

#### D2. Holehe — Email to Platform Check

- **Relies on platform-specific registration flow probing.** Platforms change their registration endpoints, error messages, and rate limiting frequently. Holehe's detection logic becomes stale quickly.
- **Binary result only** — found/not found. Does not return the username or profile URL associated with the email.
- **Some platforms block probing** and always return "email available" to prevent enumeration, causing false negatives.
- **Rate limiting.** Holehe sends many HTTP requests; aggressive use will hit rate limits on major platforms.
- **Mitigation:** Run at most once per month per email. Results are informational only.

#### D3. Nominatim — Reverse Geocoding

- **Rural and developing-world coverage is sparse.** Nominatim's address data comes from OpenStreetMap contributors. In areas with low OSM coverage, reverse geocoding returns `null` or only returns the country name.
- **1 req/sec limit is the public server limit.** For a batch of 500 GPS points, this takes ~8 minutes. For initial geocoding of a large Strava history, this is hours. Consider running Nominatim geocoding as a low-priority background task.
- **Results can change over time** as OSM data improves. Cached labels in `ana_behavioral_profiles.frequent_locations` may become stale.
- **Strava GPS points are already fuzzed** by privacy zones — Nominatim will geocode the fuzzed point, returning a nearby address that may not correspond to the actual location.

#### D4. Wayback Machine CDX API

- **Coverage is uneven.** Instagram profile pages are rarely archived because Wayback's crawler is blocked by Instagram's robots.txt. Telegram, WhatsApp, and Strava are not archived at all. GitHub profiles are archived. YouTube channels are archived.
- **Only public profile pages are archived.** Post content is not.
- **Free tier has no guarantees on availability or rate limits.** The CDX API is unsupported and may change without notice.

---

### SECTION E — Architectural Risks and Design Shortsightedness

#### E1. `ana_timeline_events` Growth is Unbounded

With 20 primary targets, active across 10 platforms, over 3+ years, `ana_timeline_events` could reach 500k–5M rows. The `(entity_id, occurred_at DESC)` index will handle most queries well, but:
- Full re-inserts on every incremental run would be O(n) where n is all events ever — the `ON CONFLICT DO NOTHING` on `(source, event_type, source_record_id)` means re-running is safe but still does full table scans for conflict detection
- Timeline queries with no entity filter (e.g., fleet view showing recent events across all primaries) will become slow
- **Mitigation:** Add a composite index `(occurred_at DESC)` for fleet-view queries. Consider PostgreSQL table partitioning by `occurred_at` (monthly) once the table exceeds 500k rows.

#### E2. Behavioral Profile Rebuild is Full, Not Incremental

`ana_behavioral_profiles` is recomputed from scratch on every nightly full-resolution run. For entities with years of timeline data, this means re-aggregating potentially 50k+ events per entity. With 20+ primaries, this is 1M+ row scans nightly.
- **Mitigation:** Switch to incremental behavioral updates in the incremental run (append new events to running aggregates using JSONB operators). Reserve full recompute for weekly runs only.

#### E3. Secondary Entity Explosion

With 5 primary targets each followed by 1,000 people on Instagram (not currently collected, but possible in future), the peripheral tier could contain 5,000+ unique usernames. The secondary promotion logic is fine, but the `ana_entity_relationships` table could accumulate hundreds of thousands of rows rapidly.
- **No archival or TTL mechanism** exists in the current design for peripheral entities or old relationships.
- **Mitigation:** Add `last_seen_at` to peripheral relationships. Entities not seen for 180 days remain peripheral and are excluded from graph analytics unless re-encountered.

#### E4. No Signal Independence Deduplication

Described in B1 above. The confidence formula needs deduplication logic: count signals by unique `(signal_type, source_platform)` pair, not raw signal count. Without this, a user with the same username on 5 platforms counts as 5 signals against a threshold of 2, bypassing the intent of the minimum-signals guard.

#### E5. No Link Retraction

Once `ana_entity_platform_links.is_confirmed = TRUE`, there is no automated pathway to retract it. If a wrong link is discovered, it must be manually corrected in the DB. For a personal tool this is acceptable, but the diagnosis process is opaque without the `ana_identity_signals` ledger.
- **Mitigation:** Add `retracted_at` and `retraction_reason` columns to `ana_entity_platform_links`. The nightly run should not re-confirm retracted links.

#### E6. `ana_behavioral_profiles.posting_hour_dist` is UTC, Not Local

All collector timestamps are stored in UTC. `posting_hour_dist` is computed from UTC hours. If the entity's timezone is UTC+8 (Singapore), a post at 11 PM local time (UTC 15:00) will appear in the 15:00 UTC bucket, not the 23:00 local bucket. The heatmap will be meaningless unless timezone normalization is applied before aggregation.
- **This is only solvable when `primary_timezone` is known.** Entities without a timezone (no Strava data, timezone inference failed) should have `posting_hour_dist` marked as `UTC_ONLY` to signal to the UI that local-time interpretation is not available.

#### E7. The Scheduler Has No Backpressure

The incremental run fires every 30 minutes regardless of how long the previous run took. If a full resolution run is in progress (nightly, potentially long), the incremental run will start concurrently, causing DB contention.
- **Mitigation:** Add a `run_lock` mechanism — the scheduler checks for an active run in `ana_analysis_runs` with `status='running'` before starting a new one. Skip the scheduled run if locked.

#### E8. No Handling of Collector Schema Changes

If a collector table is altered (new column, column renamed), the analyzer's pipeline modules that reference those columns will silently produce NULLs or fail. No schema version checking exists.
- **Mitigation:** Add schema validation on startup — check that expected collector tables and key columns exist. Fail loudly rather than silently degrade.

---

### SECTION F — Legal and Ethical Considerations (Singapore jurisdiction)

#### F1. PDPA (Personal Data Protection Act 2012)

Singapore's PDPA governs collection, use, and disclosure of personal data. Key points:
- **The personal/domestic exception (Section 4(b))** exempts personal data collected by individuals for personal/household purposes. A personal OSINT tool used for private research on known individuals likely falls here.
- **The exception does not apply** if the data is shared with third parties, used commercially, or used for surveillance in a way that causes harm.
- **Cross-correlating data from multiple platforms** to build detailed profiles is technically collection+processing of personal data. The PDPA does not prohibit this for personal use, but crossing into harassment or defamation using the derived intelligence would be actionable.

#### F2. Computer Misuse Act (CMA 1993, amended 2017)

- The collector uses legitimate platform APIs and session cookies obtained through normal login — this is not unauthorized access.
- **However:** using a secondary session/account to collect data about a target who has blocked the primary account may constitute circumventing an access control — potential CMA exposure.
- Automated scraping at high rates may also attract attention under CMA's "causing disruption" provisions if platforms detect it as an attack.

#### F3. The Data Aggregation Risk

Individual data points (a public Instagram post, a public Strava activity) are not sensitive. But the aggregated intelligence product (this person lives at X, works at Y, runs this route every Tuesday morning, is awake until 1 AM most nights, is closely connected to these 3 people) is materially more sensitive than any individual data point. This is the fundamental tension of OSINT: the inputs are public, the output is not.

**Bottom line for this tool:** personal use for known people on data they have voluntarily made public → legally defensible in Singapore. Do not share derived intelligence with third parties. Do not use the tool against strangers or in connection with any commercial activity.

---

### SECTION G — Revised Identity Signal Tiers (Post Data-Reality Audit)

The original Tier 1 signals have been materially revised:

| Signal | Original Tier | Revised Tier | Reason |
|---|---|---|---|
| `instagram_profiles.email` | Tier 1 | **REMOVED** | Never populated |
| `instagram_profiles.phone` | Tier 1 | **REMOVED** | Never populated |
| `telegram_users.phone` | Tier 1 | **REMOVED** | Never collected |
| WhatsApp JID → phone | Tier 1 | **Tier 1 (conditional)** | Valid but requires runtime parsing + format validation; LID/business JIDs produce garbage |
| `github_commits.author_email` | Tier 1 | **Tier 1 (filtered)** | Valid after `NOT LIKE '%@users.noreply.github.com'` filter |
| `github_users.email` | Tier 1 | **Tier 2** | Sparse (only public-email users); stale after first insert |
| `instagram_posts.location_lat/lng` | Tier 3 (inferred) | **REMOVED** | `download_geotags=False` hardcoded; zero location data |
| Strava GPS clusters | Tier 3 (inferred) | **Tier 3 (conditional)** | Only exists in OAuth mode; silently fuzzed by privacy zones |
| `strava_athletes.weight/height/sex` | Tier 3 (inferred) | **Tier 3 (conditional)** | Only populated in API/OAuth mode; NULL in cookie-only mode |

**Remaining strong signals (unchanged):**
- Username exact match across platforms (GitHub, Instagram, TikTok, Strava, Lemon8, YouTube, Telegram when set)
- `strava_athletes.firstname + lastname` (most reliable real name — fitness identity, rarely pseudonymous)
- `media_items.sha256` profile photo deduplication
- GitHub commit author_email (post noreply filter)
- WhatsApp JID parsed phone (post format validation)

---

## 22. Face Recognition Pipeline Design (Phase 2)

> Added 2026-06-08. This is Phase 2 scope — documented here for architectural planning.

### The Problem

~85k images + ~7.5k videos on disk. Goal: detect and match faces across all collected media to identify recurring people across platforms.

### Video Processing Pipeline

Videos cannot be fed directly to face detection. They must be decomposed into frames first.

| Step | What | Tool | Time per unit | Notes |
|---|---|---|---|---|
| 1. Frame extraction | Sample N frames per video (not every frame) | `cv2.VideoCapture` (OpenCV) | ~1 sec/video | Default: 1 frame per 5 seconds. 60-sec video = 12 frames. Configurable via `FACE_FRAMES_PER_SECOND`. |
| 2. pHash dedup | Perceptual hash each frame. Skip near-duplicates (>95% similar) | `imagehash` | <1ms/frame | Cuts frame count by 50-80% for static-camera content (talking heads, webcams). |
| 3. Face detection | Run dlib HOG detector on each unique frame. Returns bounding boxes. | `dlib` / `face_recognition` | ~0.3 sec/frame (HOG) | HOG is faster, misses some angled/small faces. CNN detector is 10x slower but more accurate. Default: HOG. |
| 4. Face embedding | Extract 128-dim ResNet embedding per detected face. | `dlib` ResNet | ~0.1 sec/face | One embedding per face per frame. |
| 5. Face matching | Compare embedding against known face index (L2 distance). | pgvector or numpy | <1ms/comparison | Threshold: 0.6 (standard dlib recommendation). |
| 6. Persist | Store embedding + bounding box + source media reference. | asyncpg → `face_embeddings` table | trivial | Link to `media_items` row for provenance. |

### Scale Estimate (current corpus)

```
Images:  ~85,000 × 0.3 sec face detection           = ~7.1 hours
Videos:  ~7,500 × 10 frames × 40% survive dedup     = ~30,000 frames
         ~30,000 × 0.3 sec face detection            = ~2.5 hours
─────────────────────────────────────────────────────────────
Total initial face scan:                              ~10 hours CPU time
```

Feasible as a weekend background job. Incremental runs process only new `media_items` since last scan.

### Configurable Knobs

```env
FACE_FRAMES_PER_SECOND=0.2          # 1 frame per 5 sec (default)
FACE_DETECTOR=hog                    # hog (fast) or cnn (accurate, 10x slower)
FACE_MATCH_DISTANCE_THRESHOLD=0.6    # L2 distance. Lower = stricter.
FACE_BATCH_SIZE=500                  # Images per incremental run
FACE_SCAN_ENABLED=false              # Master switch (Phase 2)
```

### Dependencies (all free, all CPU, no API keys)

- `dlib` — face detection + 128-dim embedding extraction
- `face_recognition` — Python wrapper around dlib (simpler API)
- `opencv-python-headless` — video frame extraction (no GUI needed)
- `imagehash` — perceptual hashing for frame dedup
- `pgvector` — vector similarity search in Postgres (already installed in collector's Postgres)

### Tables (in `unifiedanalyzer` DB)

- `face_identities` — one row per resolved face cluster (label, notes, entity_id FK nullable)
- `face_embeddings` — one row per detected face (identity_id FK, embedding vector(128), source_media_id, face_box JSONB, frame_index)
- `face_scan_progress` — tracks which `media_items` have been scanned (last_scanned_media_id, scan_status)

---

## 23. Next Steps (2026-06-08)

### Phase 1 build checklist (v0.1)
- [ ] Create `unifiedanalyzer` git repo
- [ ] Create `unifiedanalyzer` database on same Postgres instance
- [ ] Scaffold repo structure per Section 16
- [ ] Implement schema (Section 15, adapted for separate DB — drop `ana_` prefix)
- [ ] Build DB connection layer (two asyncpg pools: read-write to analyzer DB, read-only to collector DB)
- [ ] Build identity linker with Section G revised signals
- [ ] Build timeline builder (normalize all 10 platform timestamp sources)
- [ ] Build alert engine (silence gap + new-activity-after-silence + profile change)
- [ ] Build scheduler (60-min incremental + nightly full resolution + on-demand trigger)
- [ ] Build FastAPI API layer (Section 17 endpoints)
- [ ] Build React frontend (entity list, entity card, timeline, alerts home screen)
- [ ] Docker Compose for analyzer (FastAPI + frontend only, connects to existing Postgres)
- [ ] `.env.example` with all knobs
- [ ] Ship v0.1

### Collector-side checks to do (separate session, does not block analyzer):
- [ ] Run data integrity check: `SELECT COUNT(*) FROM media_items` vs file count on Z:/ (~190k files)
- [ ] Verify `strava_gps_streams` is empty (confirming cookie-only mode)
- [ ] Consider enabling Instagram `download_geotags=True` if/when 429 block clears (currently hardcoded False)

### Post-v0.1 roadmap summary
- **v0.2 (Phase 2):** Face recognition + behavioral profiling + text extraction + bio NLP
- **v0.3 (Phase 3):** Graph analytics + enrichment tools (Sherlock/Maigret/Holehe/Wayback) + map view (if geo data becomes available)
- **v1.0 (Phase 4):** Investigation mode (multi-entity correlation) + discovery mode + topic modeling + content similarity + link retraction + performance hardening
