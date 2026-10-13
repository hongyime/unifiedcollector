# Spider, For You, and Feeds Implementation Plan

This plan turns the uneven source-specific spider and feed work described in
[`architecture.md`](architecture.md) into a staged implementation. Execution
status is tracked below; implementation does not imply that a rollout gate has
been enabled.

## Execution status — 2026-10-14

| Phase | Status | Evidence / remaining gate |
|---|---|---|
| **0. Baseline and contract** | Complete | Contract and source inventory captured in this plan. |
| **1. Queue lifecycle parity** | Implemented | Commits `eb6ad9bd` and `092ec85b`; full shadow reconciliation and consumer cutover remain gated. |
| **2. Active graph adapters** | Implemented; rollout pending | Typed graph edges and fetch observations cover Instagram headless/browser, GitHub, TikTok visible-roster, and Strava visible-roster paths. GitHub shared traversal and graph writes are opt-in; legacy workers remain active. Focused assertions and transaction-only database checks passed; pytest is unavailable in the current host/runtime. Keep all flags off until bounded per-source shadow parity is measured. |
| **3. Discovery-source adapters** | Implemented; rollout pending | Typed discovery evidence and bounded shared queue writes now cover YouTube channel references, Lemon8 feed/topic/profile observations, Telegram chats/members/links, and browser-observed X/Threads/Facebook accounts. Per-platform gates remain off; legacy queues stay active. Compilation, env split validation, custom helper checks, and transaction-only live Postgres smoke passed; pytest is unavailable. A read-only ledger check confirmed the additive evidence migration is applied with the matching repository checksum. |
| **4. Feed provenance and reporting** | Implemented; live schema applied | Added a canonical surface vocabulary and persisted it on social post/media and native message writes. Browser captures report observed, stored, deduped, failed, and last-success counts through `/collectors/surface-progress`; the Coverage page shows platform/surface progress and freshness. Extension and dashboard builds, compilation, focused manual assertions, and rollback-only live Postgres write/API checks passed. A later read-only check confirmed the live migration ledger contains matching checksums for the discovery-evidence and collection-surface migrations. Pytest is unavailable. |
| **5. Feed adapter coverage** | Implemented; rollout pending | Existing browser paths retain the Phase 4 surface labels and are bounded to at most 18 scroll passes. TikTok profile fallback is capped at 100 items, Lemon8 FYP is capped at 5 pages / 120 media / 5 detail fetches, and Strava Following/date passes are capped at 10/20 pages with one retry after a 429 backoff. Instagram personalized home/Reels and YouTube personalized home are unsupported: current collectors have no bounded visible-surface adapter for them. Helper assertions, Python compilation, extension build, and read-only live schema/ledger checks passed; pytest is unavailable. No platform endpoints were contacted. |
| **6. Operations and rollout** | Implemented; rollout remains gated | `/collectors/operations` reports shared spider queue status/age, recent browser/graph/headless DLQ errors by safe category, and per-source error-query availability. The Coverage page distinguishes no errors from unavailable or partial reporting and shows stale surface freshness using recent counts plus indexed latest-record lookups. Operator-only `/collectors/feed-controls/{platform}` pauses/resumes scheduled collectors and limits cadence to 1–720 hours. Phase 2/3 shared claims stay disabled until legacy shadow parity is measured; no legacy queue was removed or cut over. Pytest is unavailable. The current live DB exceeds the bounded all-time freshness lookup and browser-error query bounds; API/UI report incomplete status rather than a false zero. |

## Current state by platform

| Platform / source | Spider and discovery today | Feed and recommendation capture today | Main gaps |
|---|---|---|---|
| **Instagram** | The headless collector has a bounded follower/following walker using `SpiderDiscover`. The browser bridge separately seeds `instagram_spider_targets` from the logged-in account and expands a capped, two-hop set. Both feed the broader profile collection path, but queue state and controls are split. | Post/media writes label profile, Explore, Reels, Stories, and tagged surfaces when the browser URL exposes them. Profile media and expiring-story work remain bounded; personalized home and Reels recommendations are not collected. | Keep personalized home/Reels unsupported until there is a bounded, visible-page capture path and a safe cadence; reconcile the two spider queues and depth/eligibility rules. |
| **TikTok** | `tiktok_spider_queue` remains active. Visible roster observations can also become typed shared graph rows behind the TikTok gate; follower visibility is limited. | Profile posts, Following, For You, and Explore captures use normalized surface labels when visible in the page context; browser scrolls and headless profile fallbacks are bounded. | Profile fallback downloads cap at 100 items. Measure bounded graph parity before cutover and retain the visible-surface limitation. |
| **Lemon8** | `lemon8_spider_queue` remains active. Feed, topic, and profile co-occurrences can write distinct shared discovery evidence behind the Lemon8 gate; these are recommendations/co-occurrences, not follows. | Headless For You, topic, and profile media/post rows retain the corresponding normalized surface. The extension registry does not manage Lemon8 as a continuous scraper tab by default. | FYP fetch caps: 5 pages, 120 media items, and 5 optional detail requests per cycle; a detail cap of zero disables detail fetches. Retain source record provenance and improve item identity when feed cards lack post IDs. |
| **YouTube** | `youtube_spider_queue` remains active. Channel references from collected records can be written as typed, source-linked shared evidence behind the YouTube gate; this is channel discovery, not a subscriber/follower BFS. | Channel uploads, videos, comments, transcripts, community posts, configured searches, and related discovery are collected. The personalized home recommendation feed is unsupported because the current collector has no bounded visible-page adapter for it. | Keep personalized home recommendations unsupported until a bounded browser-visible capture path is designed; channel/community collection does not stand in for home recommendations. |
| **GitHub** | The collector has a persistent legacy queue and bounded graph traversal across users and code relationships such as followers/following, stars, forks, and contributors. `SpiderDiscover` is available in the codebase, but the legacy implementation remains the active path. | Repository and activity tables back the dashboard's GitHub views; there is no algorithmic For You feed. | Preserve the richer code-graph edge types during any queue consolidation; avoid treating repository relationships as social follows; improve per-edge coverage and quota reporting. |
| **Strava** | `strava_spider_queue` stores athlete targets discovered from visible rosters, follows, kudos, or manual seeds. Depth and visibility depend on authenticated access and privacy settings. | Following-feed activities are labeled `following`; profile activity/media paths retain their explicit surface when known. The extension can observe route streams on open activity pages. There is no recommendation feed. | Following/date feed passes cap at 10/20 pages and stop after one repeated 429 response following the configured backoff. Retain athlete ownership and route privacy boundaries; separate following-feed coverage from profile-target collection. |
| **Telegram** | `telegram_spider_queue` remains active. Chat, member, link, mention, and forward observations can write typed shared evidence behind the Telegram gate; membership is not modeled as a follow edge. | Native message rows are labeled `message_stream`. | Measure bounded parity; keep visibility failures distinct from empty member lists. |
| **X** | The extension's visible account observations can seed a bounded X-owned shared queue behind its gate. Legacy `x_profile_targets` remains available for reconciliation. | Home, Following, and For You post/media writes retain normalized surface labels; a local-storage opt-in permits a For You pass about every fourth home cycle. | Reconcile legacy and shared target queues and use per-surface progress to measure the opt-in. |
| **Threads** | Visible account observations can seed a bounded Threads-owned queue behind its gate. For You authors remain Threads-owned and do not cross-seed Instagram. | Following and For You post/media writes retain normalized surface labels; the extension can opt into For You about every fourth home-feed cycle. | Reconcile bounded queue parity and retain the For You cross-seed guard. |
| **Facebook** | Visible browser account identifiers can seed a bounded shared queue behind its gate; no identifiers are inferred beyond captured account rows. | Visible feed and profile post/media writes retain normalized surface labels when page context is known. | Reconcile bounded queue parity and treat privacy/visibility as incomplete data. |
| **WhatsApp** | Chat and participant discovery comes from the linked-device event path and observed messages; there is no account graph spider. | Native message rows are labeled `message_stream`; message arrival remains event-driven through Baileys/RabbitMQ. | Keep message/contact discovery separate from social feed and graph work; make discovery provenance and participant identity mapping observable. |
| **Beeper / Matrix** | The Beeper REST poller discovers/syncs chats and participants; it is a mirror/backstop for native Telegram and WhatsApp plus Beeper-only networks, not an independent social graph. | Chat/message history is polled from the desktop API; Beeper is canonical for several networks that lack a native collector. | Preserve source ownership and native-versus-mirror deduplication; do not count mirrored native messages as new feed coverage. |
| **Snapchat** | No post graph or spider; current service is profile-only. | No posts or feed capture. | Out of feed/spider scope until a supported post surface and stable identifiers are defined. |
| **PayPal** | No social graph or spider; current service is profile-only. | No posts or feed capture. | Out of feed/spider scope. |
| **Airbnb** | No social graph or spider; current service is profile-only. | No posts or feed capture. | Out of feed/spider scope. |
| **Bluesky** | Current service is profile-only despite AT Protocol supporting a richer public graph; there is no post collector in this probe path. | No posts or feed capture. | Separate a future AT Protocol posts/graph feature from the profile probe; define auth, rate, and collection limits first. |
| **Pinterest** | No social graph or spider in the current profile probe. | No posts or feed capture. | Out of feed/spider scope until post/board collection has an owned design. |
| **Search, website, and exposure** | Search and website collectors follow discovered URLs/assets; exposure tools optionally spider scoped pages. These are URL/domain crawlers, not social-account graphs. | Search result records and fetched pages are discovery evidence, not personalized social feeds. | Keep the generic web crawl queue separate from platform identity queues while sharing operational concepts such as retry, age, and provenance. |

## Cross-cutting gaps

1. **Several queue models describe similar work.** `src/core/spider_discover.py`
   and `spider_queue` define reusable BFS mechanics, but platform collectors
   still use dedicated queues with different state fields. Instagram also has a
   separate browser-bridge target table. Some shared adapters are callable but
   not connected to normal scheduling.
2. **“Spider” currently means different things.** A follower edge, a YouTube
   related channel, a Lemon8 creator seen on a topic page, and a Telegram chat
   link are distinct evidence. Preserve those discovery types and confidence;
   do not flatten them into confirmed follow relationships.
3. **Feed context is uneven.** Some paths distinguish Following and For You in
   usernames or metadata, while other rows only say profile/feed. Persist a
   stable surface label and, where available, owner/account context and
   collection time on every captured post or media item.
4. **Coverage is hard to compare.** Queue depth alone does not show whether a
   platform can see its graph/feed. Report attempts, successful pages, items
   stored, dedupes, failures, queue age, last successful capture, and the
   surface (profile, Following, For You, Explore, topic, or message stream).
5. **Access boundaries must remain source-specific.** Keep current pacing,
   auth requirements, visibility caps, famous-account limits, and privacy
   handling. A missing or private surface is not a zero-edge result.

## Phased build order and estimates

Estimates are engineering person-days, including code review and focused
integration verification. They exclude waiting for account access, platform
rate-limit recovery, or upstream API changes. Work is ordered so new adapters
do not build on unmeasured coverage or ambiguous edge semantics.

| Phase | Work | Deliverable / exit criteria | Estimate |
|---|---|---|---:|
| **0. Baseline and contract** | Inventory current producers/consumers and queue tables; define canonical node identifiers, typed discovery evidence, graph edge types, feed-surface labels, and minimum progress metrics. Capture a baseline from existing operational tables before migrations. | A reviewed contract and source-to-queue map. Existing events can be classified without treating related/link discoveries as follow edges. | **2–3 days** |
| **1. Queue lifecycle parity** | Extend the shared queue contract to represent retries, next-attempt time, terminal reasons, source record, parent, hop, priority, and idempotency. Add compatibility readers/writers and reconciliation for dedicated queues; do not cut consumers over until row counts and statuses agree. | Queue state survives restart, concurrent claims do not duplicate work, and per-source rate/depth caps remain configurable. A rollback path retains current source queues. | **5–8 days** |
| **2. Active graph adapters** | Wire and normalize the graph-capable paths first: Instagram browser/headless, GitHub legacy graph, TikTok visible graph, and Strava athlete relationships. Move one source at a time behind the shared contract; keep specialized API fetching in each collector. | Each source records typed edges and a bounded hop count; duplicate seeds are idempotent; restricted/empty lists report unavailable versus empty; old queue behavior remains available during rollout. | **7–10 days** |
| **3. Discovery-source adapters** | Connect YouTube channel references, Lemon8 feed/topic creator discovery, and Telegram chat/member/link discovery to the shared lifecycle while keeping their evidence types distinct. Add bounded account queues for X, Threads, and Facebook only for identifiers exposed by existing browser captures. | Every discovered target retains its source record and confidence; For You authors do not silently become follow edges or Instagram seeds; no unbounded cross-platform fan-out. | **5–8 days** |
| **4. Feed provenance and reporting** | Add a normalized collection-surface vocabulary to post/media writes (profile, Following, For You, Explore, topic, home, search, message stream, unknown) and expose per-surface progress. Preserve existing source IDs and raw payloads. | Dashboard/API can separate Following from recommendation captures and show freshness, stored/deduped counts, failures, and last success by platform and surface. | **4–6 days** |
| **5. Feed adapter coverage** | Stabilize and label current TikTok, X, Threads, Lemon8, Strava, Facebook, and Instagram capture paths. Evaluate Instagram home/Reels capture and YouTube personalized recommendations as explicit feasibility spikes; ship only surfaces that can be captured with bounded, supportable access. | Each supported surface has a bounded cadence and item/page cap, idempotent writes, clear opt-in where appropriate, and an honest unsupported/unavailable state for inaccessible surfaces. | **8–13 days** |
| **6. Operations and rollout** | Implemented; rollout remains gated | `/collectors/operations` reports shared spider queue status/age, recent browser/graph/headless DLQ errors by safe category, and per-source error-query availability. The Coverage page distinguishes no errors from unavailable or partial reporting and shows stale surface freshness using recent counts plus indexed latest-record lookups. Operator-only `/collectors/feed-controls/{platform}` pauses/resumes scheduled collectors and limits cadence to 1–720 hours. Phase 2/3 shared claims stay disabled until legacy shadow parity is measured; no legacy queue was removed or cut over. Pytest is unavailable. The current live DB exceeds the bounded all-time freshness lookup and browser-error query bounds; API/UI report incomplete status rather than a false zero. |

**Total:** approximately **34–53 engineering person-days**. Phases 1–3 are
the Spider workstream; phases 4–6 are the feed/For You workstream. Phase 0 is
shared and should precede both.

## Recommended rollout gates

- Use feature flags per platform for shared-queue claims and For You capture.
- Keep the Phase 2/3 platform flags disabled until the legacy and shared queue
  status distributions agree for a full collection interval. The new feed
  control endpoint only changes `collection_schedules`; it does not enable
  shared claims or For You capture.
- Surface counts use the selected recent window; latest-record lookups are separately bounded. If historical freshness queries time out, the API returns an incomplete state and the Coverage page warns that older surfaces may be omitted.
- Use `/collectors/operations` to inspect shared-queue age and classified
  failures. The Coverage page also reports per-surface freshness. Queue age is
  based on `spider_queue.enqueued_at`; legacy queues remain active and their
  status accounting remains available through the backfill-equilibrium view
  until a safe indexed age source is available for each legacy schema.
- Operators can pause/resume a listed platform and set a 1–720 hour scheduler
  interval at `/collectors/feed-controls/{platform}`. Browser extensions honor
  pauses on their next config poll; the interval limits headless scheduler
  cadence. Collector-specific page/item ceilings remain hard caps.
- Run old and new queue accounting in shadow mode for one full collection
  interval; compare pending, completed, failed, duplicate, and retryable rows.
- Enable one platform at a time, starting with a source whose current behavior
  and quotas are understood. Keep a per-platform rollback switch until parity
  and freshness metrics remain stable.
- Treat recommendation and message surfaces as separate product categories.
  Do not use them as seeds for follow graphs unless the platform provides
  explicit relationship evidence.
- Keep profile-only platforms and the generic website/search crawler out of
  social For You expansion until they have a separately approved feature
  contract.
