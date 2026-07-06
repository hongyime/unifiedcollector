"""MQTT edge-chat client scaffold for the instagram_dm collector (Option A of #39).

Instagram's realtime DM channel is a Facebook-lineage MQTT connection to
``wss://edge-chat.instagram.com/chat`` — the same URL the browser extension's
DM WS-hook already probes (see extension/inject.js). What makes THIS module
different from the passive extension hook is:

  * The extension sits inside a real logged-in browser session and observes
    incoming frames. Passive, ban-safe.
  * This module authenticates AS THE MOBILE APP, brings its own credentials,
    and initiates the MQTT connection. That auth handshake is bannable if
    the fingerprint is off. See src/collectors/instagram_dm/auth.py.

The MQTT protocol on top of the WSS transport uses Facebook's ``fbns_lite``
dialect — CONNECT/CONNACK/PUBLISH mostly standard, but:

  * Some topics are pushed as Zstd-compressed Thrift blobs (not JSON).
  * The CONNECT payload carries a custom JSON auth structure inside
    the willmsg/username fields.
  * The keepalive interval is much shorter than a naive MQTT client would
    default to (Meta's server disconnects "sleepy" clients as bot signal).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class MqttClient:
    def __init__(
        self,
        auth,
        on_message: Callable[[dict], Awaitable[None]],
    ) -> None:
        self.auth = auth
        self.on_message = on_message
        self._connected = False

    async def run_forever(self) -> None:
        """Establish the MQTT connection, subscribe to the DM topics, and
        loop forever dispatching decoded messages via self.on_message.

        Reconnect strategy MUST include jittered backoff — Meta flags fast
        reconnect loops as bot behaviour. Start at 30s, exponential up to 15
        min, reset on a healthy heartbeat.
        """
        raise NotImplementedError(
            "instagram_dm.mqtt_client.MqttClient.run_forever: intentionally\n"
            "unimplemented in the scaffolding commit. Reference:\n"
            "https://github.com/mautrix/meta/tree/main/messagix/mqtt\n"
            "(implements the same protocol against IG/Messenger)."
        )
