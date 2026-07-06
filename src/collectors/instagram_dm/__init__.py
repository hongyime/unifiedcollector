"""Instagram DM collector — mobile-API isolated container (Option A of #39).

⚠️  BAN-RISK COLLECTOR — READ `README.md` IN THIS DIRECTORY BEFORE EDITING ⚠️

Why this exists as its own container/collector rather than living inside the
main `src/collectors/instagram/` module:

  * The main instagram collector uses browser-cookie scraping (session cookies
    lifted from a real logged-in Chrome/Firefox profile). Meta treats
    web-session traffic as **low-risk** — same headers as a real user.

  * This collector uses the private mobile-app API surface (private GraphQL
    endpoints, /api/v1/, MQTT edge-chat login). Meta treats non-mobile-app
    traffic on those endpoints as a **known ban signal** — device
    fingerprints, MTC seeds, TLS fingerprints, mid rotation, and password
    encryption pubkey rotation all get checked. A bug or fingerprint drift
    can burn an account.

  * The two paths must not share credentials, cookie jars, egress IP, or
    process. A ban on this container's account CANNOT be allowed to also
    kill the main instagram collector's session (which is the primary
    ban-safe IG data source today).

Isolation guarantees this module MUST preserve:

  1. **Own credentials directory.** Reads only `credentials/instagram_dm/*.txt`,
     never `credentials/instagram/*.txt`. Different logical account.
  2. **Own container.** Runs as `unifiedcollector_collector_instagram_dm`,
     scheduled/restart-independent from `unifiedcollector_collector_instagram`.
     Ban → this container dies, main IG stays green.
  3. **Own egress.** Reads `INSTAGRAM_DM_PROXY_URL` (unset by default). NEVER
     inherits PROXY_URL from the main IG container so a shared IP getting
     banned can't take out both accounts.
  4. **Own feature flag.** `INSTAGRAM_DM_COLLECTOR_ENABLED` must be `true`
     for `collect()` to do anything. Default is off. This module imports
     cleanly and register()s without any network activity.

This file is SCAFFOLDING. The real implementation (device fingerprint gen,
password RSA encryption, MQTT edge-chat login, Thrift payload decode) is left
as `NotImplementedError` in the auth / mqtt / decoder submodules — flipping
the feature flag on today will produce a clean disabled-by-flag log line, not
random errors or partial requests.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from src.core.base_collector import BaseCollector

logger = logging.getLogger(__name__)


# Feature flag — the ONLY switch that gates any mobile-API network activity.
# Default false: this module can be imported / registered / instantiated on
# every boot without touching Instagram's servers.
FEATURE_FLAG_ENV = "INSTAGRAM_DM_COLLECTOR_ENABLED"


def _feature_enabled() -> bool:
    return os.getenv(FEATURE_FLAG_ENV, "").strip().lower() in ("1", "true", "yes", "on")


# Credentials path is deliberately different from the main IG collector's so
# a mistaken import can't accidentally consume browser cookies with mobile-app
# traffic (which would immediately trip Meta's account-recovery flow).
CREDENTIALS_DIR = Path(os.getenv(
    "INSTAGRAM_DM_CREDENTIALS_DIR",
    "credentials/instagram_dm",
))


class InstagramDmCollector(BaseCollector):
    """Mobile-API-backed Instagram DM collector. Ban-risky, feature-flagged off.

    Not yet registered in ``src/collectors/__init__.py::COLLECTORS`` on
    purpose — the worker's schedule loop should not schedule this collector
    until the mobile-API auth flow is implemented and Bryan flips the flag.
    Kept as an importable module so the docker compose service can boot the
    container and log 'disabled by feature flag' cleanly when running.
    """

    SOURCE_NAME = "instagram_dm"
    # Provenance tag for media_items.ingest_path so unifiedanalyzer can tell
    # mobile-API-sourced rows apart from browser-extension rows (which use
    # ingest_path='extension') and headless-scrape rows ('headless').
    INGEST_PATH = "mobile_api"

    def __init__(self):
        super().__init__()
        self._auth = None      # instagram_dm.auth.AuthClient once implemented
        self._mqtt = None      # instagram_dm.mqtt_client.MqttClient once implemented
        self._enabled = _feature_enabled()
        self._creds_dir = CREDENTIALS_DIR
        if not self._enabled:
            logger.info(
                "instagram_dm collector: disabled by feature flag (set %s=true "
                "to enable). This message is expected — the collector will not "
                "make any network calls while disabled.",
                FEATURE_FLAG_ENV,
            )

    async def collect(self, targets: list[str]):
        """Feature-flag gate. Real implementation lives in the auth / mqtt
        submodules and is intentionally NotImplementedError until Bryan
        greenlights the mobile-API network activity."""
        if not self._enabled:
            # Fast no-op path — logged once on __init__, silent thereafter to
            # avoid drowning the worker log.
            return
        # ── flag on: real path ─────────────────────────────────────────────
        # SAFETY CHECK: reject if credentials directory doesn't exist or
        # somehow points at the main IG collector's dir. The former is a
        # missed-setup; the latter would be a catastrophic cross-contamination.
        real_creds = self._creds_dir.resolve()
        main_ig = Path("credentials/instagram").resolve()
        if real_creds == main_ig:
            raise RuntimeError(
                "instagram_dm credentials dir points at credentials/instagram — "
                "cross-contamination with the main IG collector is not allowed. "
                "Set INSTAGRAM_DM_CREDENTIALS_DIR to a separate path."
            )
        if not real_creds.exists():
            raise RuntimeError(
                f"instagram_dm credentials dir {real_creds} does not exist. "
                f"See {real_creds}/README.md for what to seed here."
            )

        # Local-only preparation: load credentials, hydrate per-account
        # device fingerprint + session state from disk. These functions are
        # all no-network — safe to call at any time. The FIRST network
        # activity happens inside auth.login() below, which is still a
        # NotImplementedError as of this commit.
        from . import credentials as _creds
        from . import device as _dev
        from . import session as _sess
        from .auth import AuthClient
        from .mqtt_client import MqttClient

        accounts = _creds.load_all(self._creds_dir)
        if not accounts:
            logger.warning(
                "instagram_dm collect() called but no credentials found in %s "
                "— see %s/README.md for the file schema. Nothing to do.",
                self._creds_dir, self._creds_dir,
            )
            return

        for acc in accounts:
            dev = _dev.load_or_create(self._creds_dir, acc.username)
            state = _sess.load(self._creds_dir, acc.username)
            logger.info(
                "instagram_dm account %s ready: device=%s.. session_valid=%s",
                acc.username, dev.device_id[:8], state.is_valid(),
            )
            if self._auth is None:
                self._auth = AuthClient(
                    creds_dir=self._creds_dir,
                    proxy_url=os.getenv("INSTAGRAM_DM_PROXY_URL"),
                )
            if self._mqtt is None:
                self._mqtt = MqttClient(auth=self._auth,
                                        on_message=self._on_mqtt_message)

            # These are the two calls with real ban-risky network activity —
            # deliberately unimplemented at scaffolding time. When implemented,
            # `auth.login(acc, dev, state)` should:
            #   - IF state.is_valid() and not state.is_stale(): reuse session
            #     tokens; DO NOT trigger a fresh login.
            #   - ELSE: RSA-encrypt the password against the daily-rotating
            #     login pubkey and POST /api/v1/accounts/login/, persisting
            #     new tokens via _sess.save() on 200 OK. On
            #     challenge_required / checkpoint_required / sentry_block:
            #     STOP. _sess.clear() the stale state. DO NOT retry — those
            #     responses are pre-ban signals.
            #   - Return an object holding cookies + MQTT connect params.
            await self._auth.login()
            await self._mqtt.run_forever()

    async def _on_mqtt_message(self, msg):
        """Callback wired from mqtt_client → into instagram_dm{,_thread} rows.
        Scaffold: format is TBD by the real MQTT/Thrift work. Structure will
        mirror the existing instagram_dm columns so unifiedanalyzer sees
        both ingest paths uniformly."""
        raise NotImplementedError(
            "instagram_dm._on_mqtt_message: implement once auth.login() +\n"
            "mqtt.run_forever() land. See src/collectors/instagram_dm/README.md "
            "for the schema mapping."
        )

    async def download_media(self, item: dict):
        """DM messages are pure text/status events on the mobile-API surface;
        media in a DM travels via a separate CDN URL that the browser
        extension already handles via the ig_ingest path. This collector
        deliberately does not pull binary media — we don't want the extra
        request volume from an already-ban-risky account. Left as a
        NotImplementedError so a caller that misuses this collector fails
        loudly instead of silently succeeding on a no-op."""
        raise NotImplementedError(
            "instagram_dm.download_media: this collector does not download "
            "binary media (that path stays on the extension). If a DM's media "
            "needs saving, hand off to the extension by fingerprint match."
        )
