# Chrome/CDP setup for UnifiedCollector

The `browser_cookie_vault` service connects to a host-side Chrome-compatible
browser through the Chrome DevTools Protocol (CDP). Configure Chrome with a
dedicated automation profile and a remote-debugging port; do not point the
collector at a personal browser profile.

## Endpoint and profile

The Compose stack sets `CHROME_CDP_URL` to
`http://host.docker.internal:9336` by default. The standalone Python module's
fallback is port 9222, so configure the URL explicitly when running outside
Compose. Keep the URL and Chrome's `--remote-debugging-port` in sync.

Example launch shape (choose a private profile directory on the host):

```sh
google-chrome \
  --remote-debugging-port=9336 \
  --user-data-dir="<dedicated-collector-profile>"
```

## Extension

Load the UnifiedCollector Bridge from the repository's `extension/` directory,
which contains `manifest.json` and the built scripts under `dist/`:

1. Open `chrome://extensions/` in the automation profile.
2. Enable Developer mode.
3. Select **Load unpacked** and choose `extension/`.

The tracked manifest currently declares extension version `1.23.119`.

## Verification and recovery

Check the configured CDP endpoint from the host:

```sh
curl -s http://localhost:9336/json/version
```

The Compose service exposes its health endpoint on port 8790. It snapshots
cookies every five minutes for the configured collector domains, including
Snapchat, Pinterest, and Airbnb, under the git-ignored
`credentials/browser_cookies/` directory. The health endpoint reports the
snapshot time, count, and auth-cookie names without exposing values. To capture
a just-completed sign-in immediately, run
`docker exec unifiedcollector_browser_cookie_vault python -m src.tools.browser_cookie_vault backup`.

Compose defaults automatic restore on when the vault service starts, unless
`BROWSER_COOKIE_VAULT_AUTORESTORE=0` disables it. A Chrome-only restart does not
restart the vault or trigger restore; restore a
known-good snapshot explicitly with
`docker exec unifiedcollector_browser_cookie_vault python -m src.tools.browser_cookie_vault restore`.
If CDP is unreachable, confirm Chrome is running with the configured port and
that the container can reach the host through `host.docker.internal`.
