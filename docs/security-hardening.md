# Security hardening rollout checklist

Apply this once per host after deploying the security-findings changes. The stack
now refuses to start without the required secrets, publishes its ports on
127.0.0.1 only, and requires the extension to send the bridge secret. Work through
the steps in order. Do not skip the preflight.

## 1. Back up before you change anything

```sh
docker compose -f docker/docker-compose.yml exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d unifiedcollector > /tmp/unifiedcollector-prehardening.sql
cp docker/.env /tmp/docker-env-prehardening
```

Keep the previous git commit SHA for rollback (step 9).

## 2. Generate the new secrets

Run this once per secret and keep the outputs somewhere safe. Never commit them.

```sh
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

Generate values for:

- `POSTGRES_PASSWORD` (strong; this also rotates the DB password)
- `RABBITMQ_PASSWORD`
- `REDIS_PASSWORD`
- `WHATSAPP_MEDIA_BRIDGE_SECRET`
- `BRIDGE_SHARED_SECRET`

## 3. Place each secret in both places it is read

Compose interpolation reads the compose project environment: `docker/.env` or a
shell export. It does **not** read `docker/env/*.env`. Runtime containers read
`docker/env/common.env`, `docker/env/broker.env`, and
`docker/env/whatsapp.env` (plus per-service files).

| Variable | Compose interpolation (`docker/.env`) | Runtime env file |
|---|---|---|
| `POSTGRES_USER` | yes | `docker/env/common.env` |
| `POSTGRES_PASSWORD` | yes | `docker/env/common.env` |
| `RABBITMQ_USER` | yes | `docker/env/broker.env` |
| `RABBITMQ_PASSWORD` | yes | `docker/env/broker.env` |
| `REDIS_PASSWORD` | yes | `docker/env/broker.env` |
| `WHATSAPP_MEDIA_BRIDGE_SECRET` | yes | `docker/env/whatsapp.env` |
| `BRIDGE_SHARED_SECRET` | yes | `docker/env/common.env` |

If any runtime env file hardcodes `DATABASE_URL`, update the password inside that
URL too.

## 4. Enable the dashboard login when you are ready

The default stays `DASHBOARD_AUTH_DISABLED=true` so the localhost-only dashboard
keeps working without a prompt. To require the SPA login, set this in
`docker/.env`:

```sh
DASHBOARD_AUTH_DISABLED=false
```

Also set these in `docker/env/dashboard.env` before recreating the dashboard:

```sh
DASHBOARD_ADMIN_USERNAME=admin
DASHBOARD_ADMIN_PASSWORD=<strong value>
DASHBOARD_JWT_SECRET=<generate with the command above>
```

Without `DASHBOARD_JWT_SECRET` the dashboard fails closed at startup.

## 5. Enter the bridge secret in the extension

1. Open the extension popup.
2. Go to Settings and paste the `BRIDGE_SHARED_SECRET` value into "Bridge secret".
   It is stored as `uc_bridge_secret` in `chrome.storage.local`.
3. Reload the extension at `chrome://extensions`.
4. Confirm the manifest version is `1.23.126` and heartbeats resume.

The value must match `BRIDGE_SHARED_SECRET` exactly. The bridge returns 401 when
the header is missing or wrong.

## 6. Preflight the Compose render

Run from the repository root:

```sh
docker compose --env-file docker/.env -f docker/docker-compose.yml config --quiet
```

This must exit 0. It must exit non-zero and name the missing variable if any of
`POSTGRES_PASSWORD`, `RABBITMQ_PASSWORD`, `REDIS_PASSWORD`,
`WHATSAPP_MEDIA_BRIDGE_SECRET`, or `BRIDGE_SHARED_SECRET` is absent.

## 7. Preflight `source_controls` before its migration applies

The production `source_controls` table already exists. The migration is
idempotent and must not recreate it, but it needs to own or be able to alter the
existing table.

```sh
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T postgres \
  psql -U "$POSTGRES_USER" -d unifiedcollector -c '\dt source_controls'
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T postgres \
  psql -U "$POSTGRES_USER" -d unifiedcollector -tAc \
  "SELECT tableowner FROM pg_tables WHERE schemaname='public' AND tablename='source_controls';"
```

The owner must match the application role. If it does not, fix ownership before
starting the scheduler or a migration run. Do not drop or rename the table.

## 8. Roll out in stages

Recreate one group at a time and check logs between groups:

```sh
for svc in rabbitmq redis wa-bridge-1 wa-bridge-2 browser_ingest dashboard scheduler; do
  docker compose --env-file docker/.env -f docker/docker-compose.yml up -d "$svc"
done
```

Then recreate the worker collectors, either all at once or one by one with
`up -d <collector_service>`. If you are also rotating `POSTGRES_PASSWORD`,
recreate `postgres` first and then every database client.

After the stack is up, rerun the CDP launcher so Chrome exposes the debug port on
host loopback only:

```powershell
pwsh scripts/start-scraper-chrome-cdp.ps1
```

## 9. Post-checks

- `curl -fsS http://127.0.0.1:8790/health` returns the vault health payload.
- Open `http://127.0.0.1:8001` and complete the dashboard login when auth is
  enabled.
- `docker compose -f docker/docker-compose.yml ps` shows every service healthy.
- The extension reports heartbeat version `1.23.126`.
- Run one Telegram cycle and one Instagram cycle. Check
  `docker compose logs --since 10m` for new errors.

## 10. Instagram discovery toggle

`INSTAGRAM_SHARED_SPIDER_ENABLED=1` is set in `docker/env/common.env.example` and
`docker/env/instagram.env.example`. This routes Instagram proximity seeds through
the unified spider queue and turns discovery on. It increases provider traffic.
That traffic is operator-accepted. Set it to `0` in the running
`docker/env/common.env` and recreate the affected services to back out just this
behavior.

## 11. Rollback

1. Revert the security-hardening commit on the host checkout (`git revert <sha>`
   or check out the previous release).
2. Recreate only the services whose configuration changed:
   `docker compose -f docker/docker-compose.yml up -d <service>`.
3. Keep paired changes together. If you revert the bridge secret check, also
   revert the extension to a build that does not send `X-UC-Bridge-Secret`, or
   leave the bridge secret set so old clients still authenticate. Reverting the
   extension alone while the bridge still requires the secret returns 401 on
   ingest.
4. If you revert the loopback bindings, recreate `postgres`, `wa-bridge-1`,
   `wa-bridge-2`, `browser_ingest`, and `dashboard` so their ports are
   republished as before.
5. Restore `/tmp/unifiedcollector-prehardening.sql` only if a migration damaged
   data. The new migration is additive, so a plain code revert is normally
   enough.
