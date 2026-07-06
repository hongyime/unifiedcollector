"""Mobile-API auth flow scaffold for the instagram_dm collector (Option A of #39).

Currently unimplemented — the whole point of the scaffold is that flipping the
feature flag today produces a clean disabled-by-flag log line instead of a
half-working request that could ping Meta's fingerprinting.

Local-only helpers already shipped:
  - credentials.load_all(creds_dir) -> list[Credentials]
    Parses `<creds_dir>/*.txt`, refuses if creds_dir == credentials/instagram/.
  - device.load_or_create(creds_dir, username) -> Device
    Stable per-account fingerprint persisted to <username>.device.json.
  - session.load / save / clear (creds_dir, username)
    Post-login state persisted to <username>.session.json.

What THIS module still needs to implement (all network-touching, ban-risky):

  1. `login(cred, device, state)`:
     * If state.is_valid() and not state.is_stale(): skip; MQTT can reuse.
     * Else: fetch the current RSA login pubkey (`X-IG-Encryption-Key-*`
       response headers on any GET to /api/v1/qe/sync/ — rotates ~daily).
     * Encrypt cred.password with that pubkey per the Instagram mobile-API
       scheme: random AES-GCM key, encrypt password with AES, wrap AES key
       with RSA-OAEP; concatenate as base64. Format documented in
       mautrix-meta messagix/session/password.go.
     * POST /api/v1/accounts/login/ with the ~30 device headers built from
       `device` (X-IG-Device-ID, X-IG-Family-Device-ID, X-IG-Android-ID,
       X-IG-Capabilities, X-IG-Connection-Type, X-Bloks-Version-Id, etc.).
     * On 200: parse the sessionid + ds_user_id + mid + ig_did + rur +
       csrftoken from response cookies + body. Update `state` + call
       session.save(). Return the artifacts.
     * On `challenge_required` / `checkpoint_required` / `sentry_block` /
       429: session.clear(); log at ERROR; raise `LoginBlocked` (a new
       exception type — DO NOT retry; those are pre-ban signals and
       further attempts on the same fingerprint escalate the ban).
     * On network / 5xx: retry with jittered exponential backoff (start
       30s, max 15min, 3 attempts max). Meta correlates fast retry loops
       as bot behaviour.

  2. `refresh()`:
     * Called when state.is_stale() but state.is_valid(). Attempts a
       lightweight session probe (GET /api/v1/users/{ds_user_id}/info/)
       to verify tokens still work.
     * On 200: bump state.last_seen_at, save.
     * On 401 / 403: session.clear() + re-run login().

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


class LoginBlocked(RuntimeError):
    """Raised when Meta responds with challenge_required, checkpoint_required,
    sentry_block, or 429. STOP. Do not retry — those responses are pre-ban
    signals; further requests on the same fingerprint escalate the ban."""


class AuthClient:
    def __init__(self, creds_dir: Path, proxy_url: str | None = None) -> None:
        self.creds_dir = Path(creds_dir)
        self.proxy_url = proxy_url
        # Populated by login() once implemented. All None until then.
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
