# docker/env — per-service scoped environment templates

> **Migration status: additive-first template phase. Steps 7+ deferred.**
>
> This sprint has only created the per-service `*.env.example` templates.
> Steps 7 through 23 of `docs/plans/per-service-env-split.md` — wiring each
> `docker/env/<service>.env` into `docker-compose.yml`, then removing the
> legacy `../.env` — are DEFERRED to a follow-up sprint. Those steps require
> the live `docker/env/*.env` files to exist on disk and require staged
> validation windows against the running stack.
>
> Before the follow-up sprint starts, the operator MUST:
>
> ```powershell
> Get-ChildItem C:\unifiedcollector\docker\env\*.env.example |
>   ForEach-Object { Copy-Item $_ ($_.FullName -replace '\.example$','') }
> ```
>
> and populate each `docker/env/<service>.env` with real values (typically
> lifted out of the current monolithic `.env`).

---

## Layout

Each file in this directory scopes a set of environment variables to the
services that need them, and only those services:

| Template                          | Services that source it |
|-----------------------------------|-------------------------|
| `common.env`                      | ALL services (shared DB + storage + timezone) |
| `instagram.env`                   | `collector_instagram`, `collector_instagram_dm`, `ig_ingest` |
| `telegram.env`                    | `collector_telegram`, `onboard_bot` |
| `telegram_notify.env`             | `scheduler`, `realtime_feed`, `watchdog`, `backup`, `collector_telegram` |
| `telegram_onboard.env`            | `onboard_bot` |
| `youtube.env`                     | `collector_youtube` |
| `strava.env`                      | `collector_lowrisk` |
| `tiktok.env`                      | `collector_tiktok` |
| `lemon8.env`                      | `collector_lemon8` |
| `whatsapp.env`                    | `collector_whatsapp`, `wa_bridge_1`, `wa_bridge_2` |
| `beeper.env`                      | `collector_beeper` |
| `github.env`                      | `collector_lowrisk` |
| `search.env`                      | `collector_lowrisk`, `collector_exposure` |
| `website.env`                     | `collector_website` |
| `exposure.env`                    | `collector_exposure` |
| `recon.env`                       | `collector_spiderfoot` (isolated — NO platform creds) |
| `dashboard.env`                   | `dashboard` |
| `realtime_feed.env`               | `realtime_feed`, `scheduler` |
| `broker.env`                      | `rabbitmq`, `redis`, `collector_whatsapp`, `postgres` |
| `browser_cookie_vault.env`        | `browser_cookie_vault` |
| `backup.env`                      | `backup` |

The full service ↔ env_file mapping lives in `docs/plans/per-service-env-split.md`
Section 3.

## Dual-source model (during transition)

Once wiring lands in the follow-up sprint, each service's `env_file:` list will
reference BOTH:

1. `../.env` — legacy monolith. Stays wired until the final subtractive step
   (plan step 21) is executed in a dedicated sprint.
2. `docker/env/<service>.env` — per-service scoped. Sourced ALONGSIDE
   `../.env`; later-listed files override earlier ones per Docker Compose
   semantics, so per-service values win.

No service loses access to any variable during the transition.

## Rotation model

Rotating an Instagram password used to require editing the same file that
holds the Postgres admin password, the Telegram API hash, and every other
collector credential. After the split:

- Rotating an IG password → edit `docker/env/instagram.env` only.
- Rotating a Telegram API hash → edit `docker/env/telegram.env` only.
- Rotating the alert bot token → edit `docker/env/telegram_notify.env` only.
- Rotating the DB password → edit `docker/env/common.env` and restart every
  service (unchanged blast radius; this rotation always touched every
  container).
- Rotating GHunt credentials → edit `docker/env/recon.env` only. The
  spiderfoot container has NO other collector credentials by design.

## Verification

Plan step 20 adds `scripts/verify_env_split.py`, which parses each service's
`env_file:` list from `docker-compose.yml`, unions the keys sourced from those
files, then greps `src/collectors/<name>`, `src/bridges/`, `src/dashboard/`,
`src/scheduler/`, `src/notifications/`, `src/tools/browser_cookie_vault.py`,
and `src/backup/` for `os.getenv(...)` and `env_int(...)` references. It
fails CI if any referenced key is missing from the sourced set for its
service.

That script does not yet exist — it lands in plan step 20 alongside the
follow-up sprint wiring.

## Files in this directory

- `*.env.example` — tracked in git, empty values, safe to commit.
- `*.env` — **NEVER tracked in git**. `docker/.gitignore` enforces this.

Rotate secrets in `docker/env/<service>.env`, not in `<service>.env.example`.
