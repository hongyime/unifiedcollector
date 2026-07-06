"""Mobile-API auth flow scaffold for the instagram_dm collector (Option A of #39).

Currently unimplemented — the whole point of the scaffold is that flipping the
feature flag today produces a clean disabled-by-flag log line instead of a
half-working request that could ping Meta's fingerprinting.

When implementing:

  1. Read credentials from `creds_dir` (typically `credentials/instagram_dm/`),
     ONE file per account. Never fall back to `credentials/instagram/`.
  2. Generate stable device identifiers (mid, ig_family_device_id,
     phone_id, family_device_id, uuid) and PERSIST them alongside the
     credential file — Meta correlates repeated device_id churn as suspicious.
  3. Fetch the current RSA login pubkey (rotates ~daily; see the
     `X-IG-Encryption-Key-*` response headers) and encrypt the password with it.
  4. POST to `/api/v1/accounts/login/` with the encrypted payload + all
     device headers. On `challenge_required`, `checkpoint_required`, or
     `sentry_block`: STOP. Do not retry. Emit a Telegram alert (that account
     is now being scrutinised; further requests risk a ban) and exit.
  5. Persist the returned session token (`sessionid` cookie value +
     `ig_did`, `mid`) so restarts don't trigger a fresh full login flow
     (which itself is a bannable signal).
  6. Return the auth artifacts needed by `mqtt_client.MqttClient` to
     establish the edge-chat connection — normally the mqtt username is
     the `ds_user_id`, password is a derived MQTT auth token, and the
     `mqttcookie` is a JSON blob of session state.

References worth reading before implementing:

  * ``mautrix-meta``: https://github.com/mautrix/meta — the closest-to-clean
    reference for the modern MQTT + password encryption flow. MIT licenced.
  * ``instagrapi``: has the mobile-API surface mapped but is more brittle
    against Meta's TLS-fingerprint checks; use for endpoint discovery, not
    verbatim copy.
  * IG's public `X-IG-App-ID`, `X-IG-Device-ID`, `X-IG-Capabilities` header
    conventions — trivially wrong values here are an instant ban.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class AuthClient:
    def __init__(self, creds_dir: Path, proxy_url: str | None = None) -> None:
        self.creds_dir = Path(creds_dir)
        self.proxy_url = proxy_url
        self.session_token: str | None = None
        self.mqtt_auth: dict | None = None

    async def login(self) -> None:
        """Perform the private mobile-API login flow. See module docstring
        for the required steps and ban-safety guardrails."""
        raise NotImplementedError(
            "instagram_dm.auth.AuthClient.login: intentionally unimplemented in\n"
            "the scaffolding commit. See src/collectors/instagram_dm/ACTIVATION.md\n"
            "for the ban-risk decision context and the implementation plan."
        )

    async def refresh(self) -> None:
        """Refresh the session before it expires. Meta doesn't publish a TTL;
        heuristic today is 're-fetch every 24h and on any 401/403'."""
        raise NotImplementedError(
            "instagram_dm.auth.AuthClient.refresh: implement alongside login()."
        )
