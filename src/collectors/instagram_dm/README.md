# Instagram DM collector — mobile-API path (Option A of issue #39)

**Status:** SCAFFOLDING ONLY — not registered in `src/collectors/__init__.py`,
feature flag off by default, all network-touching methods raise
`NotImplementedError`. This directory exists so a future implementer has a
pre-approved home for the code without needing to negotiate scope again.

## Why this is separate from `src/collectors/instagram/`

The main IG collector uses browser-cookie scraping. Meta treats that as
low-risk. This collector uses the private mobile-app API surface (`/api/v1/`,
`edge-chat` MQTT with an authed connect). Meta actively fingerprints those
endpoints and BANS accounts whose traffic doesn't match a real mobile app.

Isolation the code MUST preserve:

| Concern | Main IG collector | This collector |
|---|---|---|
| Container | `unifiedcollector_collector_instagram` | `unifiedcollector_collector_instagram_dm` |
| Credentials dir | `credentials/instagram/` | `credentials/instagram_dm/` |
| Egress | `PROXY_URL` env | `INSTAGRAM_DM_PROXY_URL` env (separate) |
| Feature flag | always on | `INSTAGRAM_DM_COLLECTOR_ENABLED` (default off) |
| Cookies | Chrome session cookies | Independent mobile app session |

Guard rails already coded in `__init__.py`:

- `collect()` no-ops when the feature flag is off.
- If the flag is on but `INSTAGRAM_DM_CREDENTIALS_DIR` resolves to
  `credentials/instagram/` (i.e. someone mis-set the env), we `RuntimeError`
  before any network call.
- Lazy import of `auth` / `mqtt_client` so a disabled boot doesn't warm up
  the (eventually) heavy dependencies.

## Ban-risk acknowledgement (BEFORE flipping the flag)

Read `ACTIVATION.md` (in this directory) end-to-end. Do not enable this
container on an account whose loss would matter. Specifically:

- Do **not** use your primary IG account. Set up a fresh account with a
  disposable phone number, use it exclusively for this collector.
- Do **not** share the egress IP with the main IG collector or the
  extension bridge. A ban on this account will ban the IP for the account,
  and re-using it invites a follow-up ban.
- Keep a manual monitoring loop for the first 72h — Meta ban decisions
  usually surface within that window as `challenge_required` / `checkpoint_required`.

## What's implemented today

Only the scaffold:
- Module structure + import hygiene
- Feature-flag gate
- Cross-contamination guard against the main IG credentials dir
- Lazy import of `auth` / `mqtt_client`
- `NotImplementedError` in the auth flow, MQTT client, and `on_message`
  callback with clear pointers to reference implementations

## What's NOT implemented

- Mobile-API device fingerprint generation
- RSA login pubkey handling / password encryption
- The actual `/api/v1/accounts/login/` flow
- MQTT connection + subscribe + reconnect-with-backoff
- Thrift payload decode for inbound DM events
- Row writing to `instagram_dm{,_thread}` (schema exists; column mapping does not)

## Where credentials live

`credentials/` is in `.gitignore` (secrets don't belong in the repo), so a
`credentials/instagram_dm/README.md` was created on-disk with the setup
instructions and stays there — see also `ACTIVATION.md` in this directory
for the same content in a tracked location.

## Implementation references (when you're ready)

- `mautrix-meta` (MIT): https://github.com/mautrix/meta — cleanest public
  reference for the current MQTT + password encryption flow.
- `instagrapi`: endpoint discovery only; its TLS fingerprint is stale.
- Meta's `X-IG-*` header conventions — see mautrix-meta `messagix/session.go`.
