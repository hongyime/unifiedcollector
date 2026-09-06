# DM Capture — Handoff for Next Coding Agent

**Date:** 2026-07-06
**Prior agent:** Claude Code (hit weekly limit mid-decision)
**Repo:** `unifiedcollector` (branch `main`, working tree clean apart from `tmp/`)
**Status:** Waiting on a *product* decision, not a coding blocker. See §6.

---

## 1. What this repo is

`unifiedcollector` is a read-only ingestion plane for 11 source platforms
(github, youtube, strava, search, website, tiktok, lemon8, whatsapp, telegram,
instagram, beeper/matrix). Everything runs as Docker Compose services sharing
one Postgres DB. Code under `src/` is bind-mounted, so changes apply on
`docker restart` / `up -d` — no image rebuild.

Three ingestion paths:

1. **Headless collectors** (`src/collectors/<source>`) — server-side scraping
   with stored cookies. Grouped for RAM: `collector_lowrisk` runs
   github+strava+search; others are per-source containers.
2. **Browser extension** (`extension/`, Chrome MV3, "UnifiedCollector Bridge",
   v1.21.5) — content-script + injected page-context hooks scrape the user's
   logged-in social sessions (IG, Threads, TikTok, Lemon8, X, Facebook) and
   POST to the `ig_ingest` bridge on `:8765`. **This is the ban-safe primary
   path for Meta/X.**
3. **Realtime messaging** — Telegram (Telethon MTProto), WhatsApp (Baileys
   bridges → RabbitMQ), Beeper (Matrix), all long-lived push connections.

Downstream is a separate service, **unifiedanalyzer**, that consumes this DB
for identity resolution, timelines, co-presence, change-tracking.

Full context: `README.md`, `AGENTS.md`, `COLLECTION_SPEC.md`.

---

## 2. The DM problem in one paragraph

Instagram and TikTok DMs are the only major content type this collector does
**not** ingest. Neither platform delivers realtime DMs over the JSON HTTP paths
the extension's `fetch`/`XHR` hooks already watch. TikTok pushes over a
"frontier" WebSocket (`wss://im-ws-…/ws/v2`, binary protobuf); Instagram pushes
over an edge-chat MQTT WebSocket (`wss://edge-chat.instagram.com/chat`,
binary). We investigated three approaches — see §3 for what shipped, §5 for
what's blocked.

---

## 3. What already shipped (do not redo)

### 3.1 #37 — Dashboard IG DM view ✅

- **Frontend:** `src/dashboard/frontend/src/features/instagram/InstagramDmPage.tsx`
  (wired in `App.tsx`).
- **Backend endpoints (FastAPI):** `src/dashboard/api.py`
  - `GET /instagram/dms/threads` (line 1204)
  - `GET /instagram/dms/messages` (below)
- **Tables** (migration `src/db/migrations/add_instagram_dm.sql`, already
  applied):
  - `instagram_dm_thread(thread_id PK, title, participants text[], owner_account, last_activity, updated_at)`
  - `instagram_dm(message_id PK, thread_id, sender_id, sender_username, text, item_type, "timestamp", is_from_me, owner_account, collected_at)`
- Current row count: **0 / 0** — page renders empty until real DM data arrives.
- Commit: `b78e79b feat(dashboard): Instagram DM view (threads + chat pane)`.

### 3.2 #38 — DM transport investigation ✅

Extension WebSocket hook (`extension/inject.js` lines 274-350) wraps
`window.WebSocket` in a **passive, send-nothing** wrapper. Per distinct socket
URL it emits one `dm_probe` message describing transport + frame shape, and
also captures any JSON frames as `<platform>_dm`. Only added on tiktok /
instagram origins.

Bridge probe endpoint: `src/bridges/ig_ingest.py::dm_probe_handler` at
`POST /social/dm-probe` (log-only, no DB write).

**Empirical evidence collected on live sessions** (bridge log, 2026-07-05/06):

| Platform  | URL                                                    | Transport | Frame kind    | Typical size | Conclusion                          |
|-----------|--------------------------------------------------------|-----------|---------------|--------------|-------------------------------------|
| tiktok    | `wss://im-ws-sg.tiktok.com/ws/v2?...`                  | ws        | arraybuffer   | ~1070 B      | Binary protobuf (real DM socket)    |
| instagram | `wss://edge-chat.instagram.com/chat?sid=...`           | ws        | arraybuffer   | 1–4 B seen   | MQTT (real DM socket); size seen so far is keepalive-only |
| instagram | `wss://gateway.instagram.com/ws/realtime?...`          | ws        | arraybuffer   | 1 B          | Keepalive channel — not DMs         |
| instagram | `wss://gateway.instagram.com/ws/streamcontroller?...`  | ws        | arraybuffer   | 1 B          | Keepalive channel — not DMs         |
| instagram | `wss://gateway.instagram.com/ws/rpsignaling?...`       | ws        | arraybuffer   | 1 B          | Keepalive channel — not DMs         |
| instagram | `wss://gateway.instagram.com/ws/lightspeed?...`        | ws        | arraybuffer   | 1 B          | Keepalive channel — not DMs         |

**Zero JSON frames on either platform.** Every payload is binary. This
confirmed the observation-only HTTP path from `direct_v2` (see
`extension/inject.js::harvestDMs`) will never see push-delivered messages —
those responses only arrive when the user manually loads inbox pages, and
even then the extension's `POST /social/dms` handler exists and works but
never fires because IG's web client doesn't hit those endpoints during
realtime chat.

Commits: `fc844a7` (tiktok WS hook), `e9f9905` (generalize to IG + probes).

### 3.3 #35 — Raw sample capture ✅ (for future decoder work)

Extension caps raw binary frames per socket (`SAMPLE_MAX=6`,
`SAMPLE_MIN_BYTES=24`, skipping 1–4-byte pings) and ships them as base64 to
`POST /social/dm-sample`. Bridge writes to
`/tmp/dm_samples/<platform>_<n>.bin` in the `unifiedcollector_ig_ingest`
container.

Current inventory (`docker exec unifiedcollector_ig_ingest ls /tmp/dm_samples/`):

- **tiktok: 55 samples** of ~1066–1077 B. First bytes of `tiktok_000.bin`:
  `0881bbebc72010efe8b987d3fbdcdf1818c09c0120012a200a08582d4d657468`
  — decodes as protobuf field 1 (varint), field 2 (varint), field 5 (length-
  delimited string starting `X-Meth…`, likely `X-Method-Version` or a header).
  **Enough to start decoder work.**
- **instagram: 1 sample**, contents `68 65 6c 6c 6f` = ASCII `hello`. That's
  a placeholder / test frame, **not** a real IG DM MQTT frame. Real IG DM
  frames have not been captured yet — every observed IG frame during the probe
  window was a 1-byte keepalive.

Commit: `8bec4f9 feat(dm): raw-sample capture on the real DM sockets for
decoder work (#35)`.

### 3.4 Bridge endpoints registered (all in `src/bridges/ig_ingest.py`)

```text
POST /social/dms          -> dms_handler         (existing IG DM ingest via direct_v2 obs)
POST /social/dm-probe     -> dm_probe_handler    (#38 probe log)
POST /social/dm-sample    -> dm_sample_handler   (#35 raw sample -> /tmp/dm_samples/)
POST /social/dm-frame     -> dm_frame_handler    (JSON frame log; unused so far — no JSON seen)
```

---

## 4. Recent commit trail

```
8bec4f9 feat(dm): raw-sample capture on the real DM sockets for decoder work (#35)
e9f9905 feat(dm): generalize DM WS hook to Instagram + add transport probes (#38)
fc844a7 feat(tiktok): observe-only WebSocket hook to investigate/capture DMs (#38)
b78e79b feat(dashboard): Instagram DM view (threads + chat pane)
a385cdf fix(beeper): bump last_synced_at every cycle so tail round-robin rotates
45c70f4 feat(instagram): DM capture via extension observation (ban-safe)
```

Extension version: **1.21.5** (auto-bumps on precommit).

Working tree is clean apart from `tmp/` scratch files (all gitignored /
untracked, unrelated to DM work).

---

## 5. What's blocked — #39 DM extraction

To get **actual DM message content** into the DB, exactly two paths remain
open, and both have real downsides:

### Option A — Mobile-API / reverse-engineered auth tokens

Reproduce IG's mobile app auth flow (device fingerprint, MID, MTC seed,
password encryption pubkey rotation, etc.) to sign requests against the
private mobile GraphQL / MQTT endpoints where actual DM payloads flow.

- **Blast radius: real ban risk.** These endpoints are heavily fingerprinted;
  authenticated requests from a non-mobile-app client are a known ban signal.
  Bryan's stated policy for this repo is Conservative on Meta/X ban risk (see
  the anti-ban language in `README.md` and the `ig_cooldown` machinery).
- Effort: high. Ongoing maintenance load as IG rotates encryption/versions.

### Option B — MQTT + Thrift decoder from captured samples

Decode the raw binary frames the extension is already siphoning (§3.3) into
structured messages, then `POST /social/dm-frame` (or a new
`/social/dm-decoded`) with the extracted `{thread_id, sender_id, text, ...}`.

- **Blast radius: zero (still observe-only). ✅**
- **Effort: high AND fragile.** IG's format is FB Messenger's MQTT-over-WSS
  wire + Thrift bodies with FB-internal enums; TikTok's is protobuf with
  unknown `.proto` definitions. Both change without notice.
- **Data problem:** we have 55 TikTok samples (workable) but only *1
  synthetic* IG sample. Real IG frames need to be captured first — which means
  Bryan actually DMing on IG web while the extension is active. Passive
  capture stays deployed and will accumulate samples during normal browsing,
  but there's no ETA.

---

## 6. The decision

Bryan's last message before the previous agent hit its weekly limit:

> "record what we know somewhere i will ask another coding agent to take over,
> then give me a prompt for that coding agent, stating what we are doing, what
> this repo does and what tasks are left"

The pending call from the previous agent, verbatim:

> defer #39 (bank the wins — dashboard view, investigation, passive capture
> stays live) or accept the ban risk of the mobile-API route?

**Recommended default: DEFER #39.** Rationale: dashboard view is done, we now
know exactly what the wire format is, passive capture accumulates samples for
free while Bryan browses. The remaining work is high-effort AND high-risk
AND was lower-priority to begin with. Nothing is lost by waiting — samples
keep piling up, and if IG behavior shifts, the extension probe will notice.

---

## 7. Tasks for the next agent

Ordered by priority. Do **not** start #39 (either path) without Bryan's
explicit go-ahead — that's the whole point of the pause.

### Priority 0 — get Bryan's decision

Read this doc back to Bryan, confirm the DEFER call, or take his override.

### Priority 1 — housekeeping (safe, small, ship without asking)

1. **Sample rotation.** `/tmp/dm_samples/` in the bridge container grows
   unbounded on tiktok. Add a per-platform rolling cap (e.g. keep newest 200)
   or move samples to a mounted host path + prune on age. Currently at 55
   tiktok samples, so nothing on fire, but this is a leak.
   Reference: `src/bridges/ig_ingest.py::dm_sample_handler` around line 808.
2. **Passive probe telemetry.** No dashboard surface for "have we seen IG DM
   frames yet?". Add a small counter panel (rows/probes in the last 24h,
   sample count per platform) on the existing dashboard so we can tell at a
   glance when real IG samples arrive. Backend query is trivial:
   count files in `/tmp/dm_samples/`, or add a lightweight `dm_probe_log` table
   if we want history.
3. **Health check for the DM WS hook.** The extension WS wrapper is installed
   once at page load; if IG/TikTok updates their bundle we won't know it
   broke. Add a heartbeat: extension emits a `dm_hook_alive` ping every N
   minutes with counters `{probes_sent, samples_shipped}`. Bridge stores last
   heartbeat per (platform, owner) so the watchdog (`src/watchdog/freshness.py`)
   can alert if it goes stale.

### Priority 2 — if Bryan greenlights #39 Option B (decoder path — no ban risk)

Do these in order; stop and check in after each step.

1. **Get real IG samples.** Ask Bryan to spend one browsing session actively
   DMing on `instagram.com` while logged in on the extension-installed
   browser. Verify samples > 24 B start appearing under
   `docker exec unifiedcollector_ig_ingest ls /tmp/dm_samples/`. Without this
   step, IG decoder work is guessing.
2. **TikTok protobuf decoder.** 55 samples are already in the container. Use
   `protoc --decode_raw` on `tiktok_000.bin` … `tiktok_054.bin` to reverse
   field layouts. Cross-reference with public TikTok "frontier" reverse
   engineering (community IM SDKs on GitHub). Aim: extract `{message_id,
   conversation_short_id, sender_uid, create_time, content_json.text}`.
3. **Wire the decoded output.** New handler
   `POST /social/dm-decoded` in `src/bridges/ig_ingest.py` that inserts into a
   new table `tiktok_dm` (mirror the schema of `instagram_dm`; add a matching
   migration file — **never edit an applied migration**, see `AGENTS.md`
   §Database rules). Update the extension: after decoding client-side, ship
   the structured payload; keep raw-sample capture as a fallback for schema
   drift.
4. **IG MQTT + Thrift decoder.** Only after (1) yields real samples. Expect
   this to be 2–3× the TikTok effort due to MQTT framing on top of Thrift
   bodies. There are open-source references (`mautrix-meta`,
   `fbchat-archive`) that document field names.
5. **Backfill.** Once decode is stable, add a one-shot: when the extension
   sees IG's `direct_v2/inbox` HTTP responses (already parsed by
   `harvestDMs` in `extension/inject.js`), also fire a manual inbox pull on
   startup. This gives historical DM data alongside the realtime WS stream.

### Priority 3 — if Bryan greenlights #39 Option A (mobile-API path — ban risk)

Do not touch this without a signed-off ban-risk decision from Bryan. If
approved: the reference implementation lives elsewhere (Instagrapi,
Instaloader) and needs adaptation. Wire it as a **new** headless collector
under `src/collectors/instagram_dm/` — **do not** mix mobile-API traffic into
the existing `instagram` collector, so a ban kills only that container. Use a
separate cookie jar and a distinct proxy egress if available.

---

## 8. Files the next agent will touch most

| Area                         | File                                                            |
|------------------------------|-----------------------------------------------------------------|
| Bridge / DM endpoints        | `src/bridges/ig_ingest.py` (search for `dm_`)                   |
| Extension DM WS hook         | `extension/inject.js` lines 188-350                             |
| Extension background/routing | `extension/background.js` (search `dm`)                         |
| Extension content bridge     | `extension/content.js` (postMessage relay to background)        |
| Extension manifest           | `extension/manifest.json` (version, host permissions)           |
| Dashboard IG DM UI           | `src/dashboard/frontend/src/features/instagram/InstagramDmPage.tsx` |
| Dashboard API                | `src/dashboard/api.py` (search `instagram_dm`)                  |
| Schema                       | `src/db/migrations/add_instagram_dm.sql` (applied — do not edit)|

---

## 9. Rules from `AGENTS.md` the next agent MUST respect

- **Never edit an applied migration** — checksums are enforced; drift halts
  every migrate-on-boot collector. Add a new file instead.
- **`TIMESTAMPTZ` for all new columns.** Never bare `TIMESTAMP`. (The existing
  `instagram_dm."timestamp"` is already `timestamptz`.)
- Bind-mounted code: changes apply on `docker restart <service>` — no image
  rebuild needed.
- Outbound is **INTENTIONALLY ABSENT**. This collector observes and archives;
  it never sends, replies, reacts, or edits on the source platform. Any
  proposal to add "send DM to reply" or similar is out of scope — build it as
  a separate service that consumes this DB.

---

## 10. How to verify state before starting

```powershell
cd C:\unifiedcollector
git log --oneline -10
git status --short

# DM sample inventory
docker exec unifiedcollector_ig_ingest sh -c "ls /tmp/dm_samples/ | awk -F_ '{print `$1}' | sort | uniq -c"

# DM table row counts
docker exec unifiedcollector_postgres psql -U collector -d unifiedcollector -c `
  "SELECT 'thread='||(SELECT count(*) FROM instagram_dm_thread)||' msg='||(SELECT count(*) FROM instagram_dm);"

# Recent probe/sample activity
docker logs --tail 300 unifiedcollector_ig_ingest 2>&1 | Select-String -Pattern "DM probe|DM sample|DM JSON frame" | Select-Object -Last 20
```

Expected today (2026-07-06): thread=0, msg=0, tiktok≈55 samples, instagram=1
placeholder sample. If those numbers have moved meaningfully, that is itself
signal — investigate before assuming the plan above is still current.
