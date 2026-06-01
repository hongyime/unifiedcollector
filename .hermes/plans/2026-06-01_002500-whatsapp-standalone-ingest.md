# Plan: WhatsApp standalone ingest -- get linked accounts' chats/groups/channels into Postgres

**Date:** 2026-06-01
**Repo:** C:\unifiedcollector
**Target:** `src/collectors/whatsapp/__init__.py`, the Baileys bridges (`wa_bridge_1/2`),
RabbitMQ, `whatsapp_*` tables.
**Status:** PLAN ONLY -- read-only investigation done. Both bridges confirmed
`whatsapp_ready: true` (2 accounts linked). All `whatsapp_*` tables exist but are EMPTY.

---

## Goal

Make the two LINKED WhatsApp accounts actually ingest: ALL their chats, groups, and
channels -> `whatsapp_chats` / `whatsapp_messages` / `whatsapp_users` + media to disk +
`media_items`, with on-demand history backfill. Currently zero rows despite both bridges
ready -- the pipe between bridge and DB is broken.

## Verified current state (the "why it's empty")

- Both bridges: `{"status":"ok","whatsapp_ready":true}` -- accounts ARE linked. ✓
- whatsapp is NOT in `COLLECTOR_DISABLED_SOURCES` (only youtube,tiktok,instagram are) --
  so the collector SHOULD be running inside the main `collector` container. ✓
- Env IS wired in the main collector:
  - `WHATSAPP_SESSION_BRIDGES_JSON = {"session_1":"http://wa-bridge-1:3001","session_2":"http://wa-bridge-2:3001"}` ✓
  - `WHATSAPP_MEDIA_BRIDGE_SECRET` set ✓
  - `RABBITMQ_URL = amqp://wac_user:...@rabbitmq:5672/` ✓
  => `_use_realtime = True`, and because RABBITMQ_URL is set it takes the **RabbitMQ
  consumer** path (`_consume_broker`), NOT the HTTP poll.
- The HTTP-poll fallback can't save us either: `GET /messages/recent` on the bridge returns
  **404** -- that endpoint doesn't exist on this bridge build.
- Therefore ingest depends entirely on **the bridge PUBLISHING message events to the
  `whatsapp.events` RabbitMQ exchange**, and the collector consuming queue
  `unifiedcollector.messages` (routing key `messages.#`). Empty tables => one of these
  links is broken.

### The collector consumer (confirmed in code)
`_consume_broker` (lines 181-202): declares topic exchange `whatsapp.events`, queue
`unifiedcollector.messages`, binds `messages.#`, processes each event via
`_handle_message_event` -> `_upsert_chat` / `_track_user_profile` / `_upsert_message` /
media download. The consumer logic is sound. So the prime suspect is the **bridge not
publishing** (or publishing to a different exchange/routing key, or to a different
RabbitMQ vhost/user).

## Diagnostic plan (do FIRST -- find the exact break before changing anything)

1. **Is the whatsapp collector task even alive?** Check main collector logs for
   `WhatsApp RabbitMQ connected` vs `no collection mode available` vs a crash. Confirm the
   worker launched a `whatsapp` source task (it's a single-source? or part of main?).
   - If whatsapp isn't in the main worker's source list at all, that's the bug (env is set
     but the source never starts).
2. **Is RabbitMQ reachable + what's in it?** Hit the RabbitMQ management API / `rabbitmqctl`
   inside the `rabbitmq` container: list exchanges (is `whatsapp.events` declared?), list
   queues (does `unifiedcollector.messages` exist? message count? consumers?), check the
   `wac_user` vhost. A queue with messages piling up + 0 consumers = collector not
   consuming. A queue with 0 messages = bridge not publishing.
3. **Is the bridge publishing?** Inspect the bridge container: its env (RABBITMQ url/vhost,
   exchange name, whether publish is enabled), and its logs for publish attempts/errors.
   Bridge source is archived at `archive/whatsappcollector/` (and the running image) --
   read `services/wa-client-ts/src/event_handlers/messages.ts` equivalent to confirm the
   exact exchange + routing key it publishes to, and that publishing is turned on.
4. **Send a test message** to one linked account from your phone; watch (a) bridge log
   ("message received/published"), (b) RabbitMQ queue depth, (c) collector log
   ("Broker message processing"), (d) `whatsapp_messages` count. Whichever stage stays at
   zero is the break.

## Proposed approach (branches by diagnostic outcome)

- **If bridge isn't publishing** (queue empty): fix/enable publishing in the bridge --
  correct exchange name (`whatsapp.events`), routing key (`messages.<event>`), and the
  same RabbitMQ vhost/credentials the collector uses. Rebuild bridge image (Pattern B).
- **If queue has messages but no consumer** (collector not consuming): the whatsapp source
  task isn't running -- ensure the worker starts a `whatsapp` collector (add to the worker
  source list / single-source container like youtube/tiktok), and that
  `_init_broker` succeeds (aio_pika installed in the image, URL correct).
- **If consuming but not persisting** (errors in `_handle_message_event`): inspect the
  per-event exception (verbose logging), check the `whatsapp_messages` schema matches the
  INSERT (columns `platform_message_id, chat_id, sender_id, from_me, text,
  media_mime_type, timestamp, metadata`). Schema drift => fix migration or INSERT.
- **History backfill:** once live messages flow, drive `backfill_chat(jid, ...)` across all
  dialogs (the bridge `/backfill-request` endpoint) to pull history, rate-limited by
  `WHATSAPP_BACKFILL_REQ_PER_MIN` (default 5) and capped at
  `WHATSAPP_MAX_BACKFILL_AGE_DAYS` (90). Need a chat enumeration: does the bridge expose a
  "list all chats" endpoint, or do chats only appear as messages arrive? If the former,
  enumerate + backfill; if the latter, chats populate organically + backfill on first sight.

## Decoupling (matches the per-source-container pattern)

Like `collector_youtube` / `collector_tiktok`, consider a dedicated `collector_whatsapp`
container running `python -m src.main worker --source whatsapp` so WhatsApp's long-lived
broker consumer + media archival loop don't share the main collector's lifecycle (and so a
WhatsApp wedge doesn't take down telegram/etc.). Add `whatsapp` to main's
`COLLECTOR_DISABLED_SOURCES` and give the new container the same env. Decide during build.

## Step-by-step

1. Diagnostic steps 1-4 above (read-only + one test message). Identify the break.
2. Fix the identified stage (bridge publish / collector consume / persist schema).
3. Verify live ingest: test message -> row in `whatsapp_messages` within seconds.
4. Enumerate dialogs for both sessions; kick off rate-limited history backfill.
5. Verify media: send an image -> file on disk under
   `media/.../session_*/image/` + `media_items` row + decrypt via bridge `/media/decrypt`.
6. (Optional) split into `collector_whatsapp` container.
7. Wire the dashboard WhatsApp Users/Links/Stats to show real counts (depends on the
   dashboard overhaul plan's B2 fix so `/whatsapp/users` stops 500ing).
8. Bake (rebuild affected images), commit, push.

## Files likely to change

- `src/collectors/whatsapp/__init__.py` (consumer/persist fixes, backfill driver)
- Bridge source (publish config) -- location: running image + `archive/whatsappcollector/`
  reference; confirm the LIVE bridge image's source path.
- `docker/docker-compose.yml` (RabbitMQ wiring, optional `collector_whatsapp` service,
  env parity)
- Possibly a migration if `whatsapp_*` schema drifted from the INSERTs.
- DB: none to delete (tables empty).

## Tests / validation

- End-to-end: phone -> bridge log -> RabbitMQ queue -> collector log -> `whatsapp_messages`
  row. Media: image -> disk + `media_items`.
- `ast.parse` + `ruff E9,F821` on touched .py.
- Confirm both sessions (2 accounts) ingest independently.
- Backfill: confirm historical messages appear, rate-limit respected, no FloodWait/ban.

## Risks, tradeoffs, open questions

- **Ban risk:** aggressive history backfill on a personal WhatsApp account can trip
  anti-abuse. Keep `WHATSAPP_BACKFILL_REQ_PER_MIN` low (5), backfill depth bounded (90d),
  stagger across the 2 sessions. WhatsApp is less forgiving than Telegram here.
- **Bridge image source of truth:** need to confirm whether the running bridge is built
  from `archive/whatsappcollector/` or a separate live path -- editing the wrong copy =
  Pattern B revert.
- **OPEN: does the bridge publish at all in this build?** The 404 on `/messages/recent`
  suggests a trimmed bridge; must confirm the RabbitMQ publish path exists and is enabled.
- **Channels (newsletters):** WhatsApp "channels" are a distinct entity from chats/groups;
  confirm Baileys in this bridge surfaces channel messages and that `_upsert_chat`'s
  is_group/`@g.us` logic handles `@newsletter` JIDs (it currently only special-cases
  `@g.us`). May need a third chat-type.
- **face embeddings tables** (`wa_face_embeddings`, `wa_face_identities`) exist -- out of
  scope here; note they're the WA-side of facetracker integration, not message ingest.
