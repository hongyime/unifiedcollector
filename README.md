# unifiedcollector

Unified ingestion plane for 11 source platforms (github, youtube, strava, search,
website, tiktok, lemon8, whatsapp, telegram, instagram, beeper/matrix). Read-only by
design. Feeds a downstream **unifiedanalyzer** (separate service) that does identity
resolution, face clustering, timelines, co-presence and change-tracking.

## Architecture

Everything runs as Docker Compose services sharing one Postgres DB. Code lives under
`src/` and is **bind-mounted** into the containers, so changes apply on
`docker restart` / `up -d` **without an image rebuild** (the vhdx must not grow).

### Collection paths (three ways in)
- **Headless collectors** (`src/collectors/<source>`, `python -m src.main worker
  --source X`) — server-side scraping with stored cookies: `instagram`, `tiktok`,
  `lemon8`, `youtube`, `strava`, `github`, `search`, `website`. Grouped for RAM:
  `collector_lowrisk` runs github+strava+search; the rest are per-source containers.
- **Browser extension** — "UnifiedCollector Bridge" (Chrome MV3, `extension/`). A
  continuous in-tab content-script loop scrapes your *logged-in* social sessions
  (`instagram`, `threads`, `tiktok`, `lemon8`, `x`, `facebook`), following-first, and
  POSTs to the `ig_ingest` bridge. This is the ban-safe primary path for Meta/X.
- **Realtime messaging** — push/event sources that park forever:
  - `telegram` — Telethon MTProto, 4 accounts, live `NewMessage`/edits/deletes/
    reactions + full-history backfill (to 2018 = account age).
  - `whatsapp` — Baileys bridges (`wa_bridge_1/2`, TS, `src/bridges/whatsapp`) →
    RabbitMQ → `collector_whatsapp` consumer. On-demand deep history + live + revokes.
  - `beeper` — Matrix/Beeper, multi-network (Facebook/Discord/etc), reaches ~2011.

### Support services
- **`ig_ingest`** (aiohttp, :8765) — receives extension data → `media_items` + posts +
  `social_users`; live IG cookie sync (`/social/cookies`), anti-ban cooldown
  coordination (`/social/ig_cooldown`), Threads↔Instagram handle cross-pollination.
- **`dashboard`** (React/Vite + FastAPI, :8700) — collection *operations* (collector
  health, media browser, per-source stats, live status). Stays in its lane; the
  analyzer (:8002) owns investigation/identity.
- **`watchdog`** (`src/watchdog/freshness.py`) — data-freshness safety net: restarts a
  realtime collector's container if its newest row goes stale (the container
  healthcheck only tests HTTP, so a dead MTProto/WhatsApp connection would otherwise
  sit silently — telegram once ran dead 26h, whatsapp 4d).
- **`realtime_feed`** (`src/notifications/realtime_feed.py`) — every newly-inserted
  `media_items` row is fire-and-forget enqueued to Redis (`uc:realtime_post_feed`);
  this drain sends a per-post Telegram message with local-file multipart upload,
  token-bucket rate-limit (default 6/min), and 7-day sha256 dedupe. `sent to
  telegram: ok=<bool> ...` INFO line per item. Companion hourly digest lives in
  `src/notifications/status.py`; 15-min delta in `status_delta.py`.
- **`browser_cookie_vault`** (`src/tools/browser_cookie_vault.py`, :8790) — snapshots
  every social cookie from host Chrome via CDP every 5 min (default), keeps 10
  rotating snapshots + a `latest.json`, and on container start (with
  `BROWSER_COOKIE_VAULT_AUTORESTORE=1`) pushes them back into Chrome so a profile
  wipe / clean-cookie event no longer strands whole collectors. Health at
  `/health`; last-backup timestamp is what the watchdog trusts.
- **infra** — `postgres` (unified DB), `rabbitmq` (messaging broker), `redis` (dedup/
  cache), `scheduler`, `onboard_bot`, `backup`.

### Key mechanisms
- **`media_items`** — one table for all downloaded media. Dedup by `(source,
  content_id)` UNIQUE **and** cross-collector `sha256`. Flat dated naming
  `<YYYYMMDD>_<platform>_<user>_<kind><id>.<ext>`.
- **`source_url` contract** — every media_items row carries `source_url`, the
  canonical human-openable URL for the media's source page (video / post /
  profile). Each collector derives it via a `_build_<source>_source_url(item)`
  @staticmethod called from its `insert_media_item(...)`. Platforms with no
  public URL (WhatsApp media, private Telegram DMs) use a stable URI scheme
  (`whatsapp://<chat_jid>/<msg_id>`) or NULL respectively. Contract enforced
  by the docstring on `src/core/base_collector.py::insert_media_item` and by
  fresh-inflow monitoring on the dashboard.
- **`ingest_path`** — provenance tag on every row: `headless` (server-side
  scrape), `extension` (browser-observed), `messaging` (realtime
  telegram/whatsapp/beeper), `mobile_api` (scaffolded, off by default).
  Default from the collector's `INGEST_PATH` class attribute; extension
  bridge sets its own inline.
- **`social_users`** — universal cross-platform person registry (usernames, ids,
  profile photos, contexts like follow/comment/tagged/author).
- **Deletion tracking** — for the analyzer's "what changed since last viewed":
  telegram `metadata->>'deleted'`+`deleted_at`, whatsapp/beeper `is_deleted`+
  `deleted_at` (partial-indexed).
- **Anti-ban** — headless: exponential 429 backoff (15m→4h, persisted); extension:
  persistent throttle walls surviving tab refresh; both cooperate via `ig_cooldown`.
- **Migrations** — migrate-on-boot with a `schema_migrations` ledger. **Never edit an
  applied migration** (checksum drift bricks every migrate-on-boot collector); add a
  new file.

## Outbound functionality — intentionally absent

Unified collector is read-only by design. The following toolkit features were
**INTENTIONALLY DROPPED** during the Wave 2 port — they are not regressions and
should not be "restored" as missing functionality:

- **Telegram**: `shared/media_uploader.py`, `src/managers/resender.py`,
  `src/managers/send_photos.py`
- **WhatsApp**: `services/bulk_sender/`
- **Generic (across platforms)**: send / reply / react / edit / delete / typing
  indicators / mark-as-read / bot-command-handler

Rationale: **collection without contamination.** This service observes and
archives; it never writes back to the source platform. Mixing outbound
primitives into the collector creates ambiguity about whether a message in the
unified DB originated from a real user or from our own automation, and
materially raises the blast radius of any bug or credential leak.

Future maintainers: if outbound is needed for a specific use case, build it as
a **separate service that consumes the unified DB** — do not embed it in the
collector. The toolkits archived under `archive/` retain the
original outbound implementations as reference.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
