# WhatsApp Bridge (native to unifiedcollector)

WhatsApp has no Python client library, so unlike every other source it needs a
small TypeScript gateway process (Baileys / `@whiskeysockets/baileys`) that talks
to WhatsApp Web and republishes events onto RabbitMQ. The Python collector at
`src/collectors/whatsapp/` is the *consumer*; this is the *producer/bridge*.

## Data contract

- Exchange: `whatsapp.events` (topic, durable)
- Routing keys: `messages.text`, `messages.media`, `messages.status`,
  `messages.history` (batch), plus `session.*`, `contacts.update`, `groups.update`.
- The consumer binds queue `unifiedcollector.messages` with `messages.#`, so all
  message + history traffic uses the `messages.*` namespace. (The old standalone
  bridge published `msg.*`, which never routed here -- that was a real bug.)
- Payloads are flat and include both canonical fields (`message_type`) and the
  raw Baileys aliases the consumer reads (`messageType`/`media_type`,
  top-level `mediaKey`/`directPath`/`media_url`). See `src/utils/normalize.ts`.

## History backfill

`SYNC_FULL_HISTORY` defaults to `true` (overridable). On first connect WhatsApp
pushes `messaging-history.set`; `event_handlers/history.ts` normalizes + batches
those into `messages.history`, and the consumer unpacks the batch. Progress is
logged every 60s and watermarked to `auth_info/history_watermarks.json`.

## Session / auth

Auth is a Baileys multi-file state in `AUTH_STORAGE_PATH` (default
`./auth_info/<SESSION_NAME>`), mounted from the host at `sessions/whatsapp/<account>`.
Keep these files intact across restarts/migrations to preserve the WhatsApp link.
If they are cleared or corrupted, the bridge logs a QR code to re-pair.

## Env

| var | default | purpose |
|-----|---------|---------|
| `SESSION_NAME` | `default` | session/account id (e.g. `account1`) |
| `AUTH_STORAGE_PATH` | `./auth_info/<SESSION_NAME>` | Baileys auth dir |
| `WHATSAPP_RABBITMQ_URL` / `RABBITMQ_URL` | -- | broker URL (required) |
| `SYNC_FULL_HISTORY` | `true` | backfill history on connect |
| `PAIRING_CODE_PHONE` | -- | optional pairing-code login instead of QR |
| `LOG_LEVEL` | `info` | pino level |

## Build / run

Built and run via the repo `docker-compose.yml` (services `wa-bridge-1/2`),
build context `./src/bridges/whatsapp`. Local dev: `npm install && npm run build && node build/index.js`.
