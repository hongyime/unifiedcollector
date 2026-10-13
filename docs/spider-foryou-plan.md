# Spider, For You, and Feeds Implementation Plan

This plan turns the uneven source-specific spider and feed work described in
[`architecture.md`](architecture.md) into a staged implementation. It is a plan
only; no spider or feed feature is built here.

## Current state by platform

| Platform / source | Spider and discovery today | Feed and recommendation capture today | Main gaps |
|---|---|---|---|
| **Instagram** | The headless collector has a bounded follower/following walker using `SpiderDiscover`. The browser bridge separately seeds `instagram_spider_targets` from the logged-in account and expands a capped, two-hop set. Both feed the broader profile collection path, but queue state and controls are split. | The extension collects target profile timelines, tagged media, Stories, and Reels/highlights. It does not collect the personalized home/Reels recommendation feed. | Reconcile the two spider queues and depth/eligibility rules; measure graph coverage and API access failures; define whether a visible home/Reels surface is supportable without adding high-risk request patterns. |
| **TikTok** | `tiktok_spider_queue` is processed by the headless collector. The extension observes users from visible lists and posts; follower visibility is limited. A shared-spider adapter exists, but its `spider_related_creators` entry point is not wired into the normal cycle. | The extension's default tab is Following. Expanded-tab mode can open `/foryou` and `/explore`; collection is browser-visible and is not a complete feed archive. | Wire one bounded graph/discovery path, normalize relationship provenance, and make the optional surfaces measurable and operator-controlled. |
| **Lemon8** | The headless collector enqueues creators and tags observed in feed, topic, and profile pages in `lemon8_spider_queue`. Related-creator discovery is an approximation, not a follow graph; the shared-spider entry point is callable but not scheduled from the main loop. | The headless collector scrapes For You with an API attempt and web fallback, and can scrape topic pages. The extension registry does not manage Lemon8 as a continuous scraper tab by default. | Make feed-derived discovery and queue draining one explicit bounded path; distinguish recommendations, topics, and profile pages in stored provenance; improve item identity when feed cards lack post IDs. |
| **YouTube** | `youtube_spider_queue` expands channels discovered from search, related/reference data, and collected records. It is channel discovery, not a subscriber/follower BFS. | Channel uploads, videos, comments, transcripts, community posts, configured searches, and related discovery are collected. The personalized home recommendation feed is not collected. | Record discovery source and refresh/retry state consistently; define an explicit boundary for recommendations because the current API/channel paths do not represent a personalized home feed. |
| **GitHub** | The collector has a persistent legacy queue and bounded graph traversal across users and code relationships such as followers/following, stars, forks, and contributors. `SpiderDiscover` is available in the codebase, but the legacy implementation remains the active path. | Repository and activity tables back the dashboard's GitHub views; there is no algorithmic For You feed. | Preserve the richer code-graph edge types during any queue consolidation; avoid treating repository relationships as social follows; improve per-edge coverage and quota reporting. |
| **Strava** | `strava_spider_queue` stores athlete targets discovered from visible rosters, follows, kudos, or manual seeds. Depth and visibility depend on authenticated access and privacy settings. | The headless collector reads the authenticated following-activity feed and stores activities; the extension can observe route streams on open activity pages. There is no recommendation feed. | Unify queue retry/age reporting; retain athlete ownership and route privacy boundaries; separate following-feed coverage from profile-target collection. |
| **Telegram** | `telegram_spider_queue` expands chats from mentions, member lists, forwards, links, and manual seeds. Chat discovery is not a follower graph. | Messages arrive through the event-driven Telethon collector; the chat/message history is the source feed. | Make chat-discovery evidence and visibility failures explicit; do not model group membership as a universal social follow edge. |
| **X** | The extension can observe people and visible follower/following lists, but there is no dedicated durable X graph queue comparable to the platform queue tables. | The extension captures visible home-timeline posts. Following is the default; a local-storage opt-in permits a For You pass about every fourth home cycle. | Add bounded queueing for eligible observed accounts; surface the opt-in and label Following versus For You in persisted records and operations views. |
| **Threads** | Visible account observations can be recorded; Following-feed authors may cross-seed Instagram's queue. For You observations are intentionally excluded from that cross-seed. There is no dedicated Threads spider queue. | The extension captures visible Following by default and can opt into For You about every fourth home-feed cycle. | Add a Threads-owned bounded queue and provenance; retain the guard against promoting algorithmic For You authors into Instagram seeds. |
| **Facebook** | The extension records visible users, but there is no durable Facebook graph queue. | The extension captures visible feed/profile media from the open page; there is no separate, normalized recommendation-feed mode. | Add queueing only for identifiers and relationships the page exposes; identify surfaces explicitly and treat privacy/visibility as incomplete data. |
| **WhatsApp** | Chat and participant discovery comes from the linked-device event path and observed messages; there is no account graph spider. | Messages are event-driven through Baileys/RabbitMQ. This is a message stream, not an algorithmic recommendation feed. | Keep message/contact discovery separate from social feed and graph work; make discovery provenance and participant identity mapping observable. |
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
| **6. Operations and rollout** | Add queue-age and surface-freshness views, error classifications, per-platform pause/limit controls, rollout notes, and migration cleanup only after parity has held. | Operators can identify stale queues versus unavailable source surfaces; rollout can be paused or rolled back per platform; no queue is dropped before reconciliation. | **3–5 days** |

**Total:** approximately **34–53 engineering person-days**. Phases 1–3 are
the Spider workstream; phases 4–6 are the feed/For You workstream. Phase 0 is
shared and should precede both.

## Recommended rollout gates

- Use feature flags per platform for shared-queue claims and For You capture.
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
