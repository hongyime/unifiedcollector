"""Opt-in Tor SOCKS5 proxy wrapper for outbound HTTP.

Used ONLY by the github, search, and website collectors. Routing
Instagram / TikTok / Lemon8 / Strava traffic through Tor exit nodes
amplifies their ban rate (those services aggressively block known Tor
egress IPs), so this module enforces a consumer allowlist at the
factory entry point.

Reachability
------------
The Tor sidecar lives at ``unifiedcollector_tor`` on the compose
network and exposes:

* ``tor:9050`` — SOCKS5 (default, unauthenticated)
* ``tor:9051`` — Control port (NEWNYM circuit rotation)

Hosts/ports are overridable via env (``TOR_SOCKS_HOST``,
``TOR_SOCKS_PORT``, ``TOR_CONTROL_HOST``, ``TOR_CONTROL_PORT``,
``TOR_CONTROL_PASSWORD``).

Opt-in pattern
--------------
``TOR_PROXY_ENABLED=1`` in the environment toggles real Tor routing.
When unset, ``get_proxied_client()`` returns a transparent direct
client. This means a collector can call this module unconditionally
and get tor-or-not based on env — no conditional plumbing in the
collector itself.

API surface
-----------

* ``get_proxied_client(consumer, timeout=30) -> httpx.AsyncClient``
* ``async new_circuit() -> bool``  — request a fresh exit IP
* ``async is_healthy() -> bool``   — verify IsTor=true via check.tp.org
* ``DirectClient`` / ``TorClient`` — thin wrappers over httpx with a
  shared minimal interface (``get`` / ``post`` / ``aclose``).

Consumer allowlist
------------------
``_ALLOWED_CONSUMERS = {'github', 'search', 'website'}``. Any other
value passed to ``get_proxied_client(consumer=...)`` raises
``ValueError`` immediately. This is the architectural guard against
accidental mass-routing of social media collectors through Tor.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

# Hard allowlist. Edits to this set are an architectural change — keep
# in sync with PORT_PLAN_v2 §"Tor proxy scope".
_ALLOWED_CONSUMERS: frozenset[str] = frozenset({"github", "search", "website"})

# Tor torproject.org check endpoint. Returns ``{"IsTor": bool, "IP": "..."}``.
_TORCHECK_URL = "https://check.torproject.org/api/ip"

# NEWNYM cooldown — Tor itself rate-limits NEWNYM to once every 10s by
# default (MaxClientCircuitsPending / NewCircuitPeriod). We enforce a
# soft client-side cooldown to avoid churning the control port.
_NEWNYM_MIN_INTERVAL_S = 10.0


def _env(name: str, default: str) -> str:
    val = os.getenv(name)
    return val if val else default


def is_enabled() -> bool:
    """Return True iff TOR_PROXY_ENABLED=1 in the environment."""
    return os.getenv("TOR_PROXY_ENABLED", "0") == "1"


def _socks_host() -> str:
    return _env("TOR_SOCKS_HOST", "tor")


def _socks_port() -> int:
    return int(_env("TOR_SOCKS_PORT", "9050"))


def _control_host() -> str:
    return _env("TOR_CONTROL_HOST", "tor")


def _control_port() -> int:
    return int(_env("TOR_CONTROL_PORT", "9051"))


def _control_password() -> str:
    # Empty string means cookie/no-auth. The dperson/torproxy default
    # config is unauth on the internal docker network.
    return os.getenv("TOR_CONTROL_PASSWORD", "")


def _socks_url() -> str:
    """Return the SOCKS5 URL string for httpx ``proxy=`` arg.

    No credentials embedded by default (the sidecar doesn't require
    them on the docker-internal network). If ``TOR_SOCKS_USERNAME`` /
    ``TOR_SOCKS_PASSWORD`` are set, they're embedded — Hermes will
    redact them in display logs.
    """
    user = os.getenv("TOR_SOCKS_USERNAME", "")
    pw = os.getenv("TOR_SOCKS_PASSWORD", "")
    host = _socks_host()
    port = _socks_port()
    if user and pw:
        return f"socks5://{user}:{pw}@{host}:{port}"
    return f"socks5://{host}:{port}"


# --------------------------------------------------------------------------- #
# Allowlist guard
# --------------------------------------------------------------------------- #


def _check_consumer(consumer: str) -> None:
    """Raise ValueError if consumer is not on the Tor allowlist.

    Raised eagerly at client construction time — *before* any network
    I/O — so a misconfigured collector fails on startup, not mid-run.
    """
    if consumer not in _ALLOWED_CONSUMERS:
        allowed = sorted(_ALLOWED_CONSUMERS)
        raise ValueError(
            f"Tor proxy is not allowed for consumer={consumer!r}. "
            f"Allowed consumers: {allowed}. Routing this collector "
            "through Tor would amplify ban rates from social-media "
            "platforms that aggressively block Tor exit IPs."
        )


# --------------------------------------------------------------------------- #
# Client wrappers
# --------------------------------------------------------------------------- #


class _BaseClient:
    """Minimal common interface — get/post/aclose."""

    _client: httpx.AsyncClient

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._client.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._client.post(url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "_BaseClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    @property
    def httpx_client(self) -> httpx.AsyncClient:
        """Escape hatch — return the underlying httpx.AsyncClient.

        Useful when a third-party SDK (e.g. PyGithub's HTTP backend)
        wants the raw httpx client.
        """
        return self._client


class DirectClient(_BaseClient):
    """Plain httpx.AsyncClient — no proxy. Used when TOR_PROXY_ENABLED!=1."""

    def __init__(self, timeout: float = 30.0, **kwargs: Any) -> None:
        self._client = httpx.AsyncClient(timeout=timeout, **kwargs)


class TorClient(_BaseClient):
    """httpx.AsyncClient routed through the Tor SOCKS5 sidecar."""

    def __init__(self, timeout: float = 30.0, **kwargs: Any) -> None:
        proxy = _socks_url()
        # httpx 0.27+ uses ``proxy=`` (singular). Older code used
        # ``proxies=`` — we deliberately use the new arg.
        self._client = httpx.AsyncClient(timeout=timeout, proxy=proxy, **kwargs)
        self._proxy_url = proxy


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def get_proxied_client(
    consumer: str,
    timeout: float = 30.0,
    **kwargs: Any,
) -> _BaseClient:
    """Return an httpx-compatible async client for ``consumer``.

    * Enforces the consumer allowlist (ValueError on violation).
    * If TOR_PROXY_ENABLED=1, returns a ``TorClient``.
    * Otherwise, returns a ``DirectClient`` (transparent passthrough).

    Parameters
    ----------
    consumer:
        One of {'github', 'search', 'website'}. Anything else raises
        ValueError immediately — the architectural guard.
    timeout:
        httpx timeout in seconds. Tor adds latency; 30s default
        accommodates exit-node variance.
    **kwargs:
        Forwarded to ``httpx.AsyncClient(...)``.

    Returns
    -------
    _BaseClient
        ``TorClient`` when enabled, ``DirectClient`` otherwise. Both
        expose ``get`` / ``post`` / ``aclose`` and an
        ``httpx_client`` escape hatch.
    """
    _check_consumer(consumer)
    if is_enabled():
        logger.debug("tor_proxy: routing consumer=%s through Tor", consumer)
        return TorClient(timeout=timeout, **kwargs)
    logger.debug("tor_proxy: passthrough (TOR_PROXY_ENABLED!=1) consumer=%s", consumer)
    return DirectClient(timeout=timeout, **kwargs)


# --------------------------------------------------------------------------- #
# Control port — NEWNYM circuit rotation
# --------------------------------------------------------------------------- #

# Module-level state for NEWNYM cooldown. Single-process scope is
# fine — collectors don't share processes.
_last_newnym_ts: float = 0.0
_newnym_lock: Optional[asyncio.Lock] = None


def _get_newnym_lock() -> asyncio.Lock:
    global _newnym_lock
    if _newnym_lock is None:
        _newnym_lock = asyncio.Lock()
    return _newnym_lock


def format_control_commands(password: str) -> list[bytes]:
    """Return the byte-sequence to send the Tor control port for NEWNYM.

    Pulled out as a pure function so unit tests can verify wire
    format without opening a socket.

    Format (from torspec/control-spec):
        AUTHENTICATE "<password>"\\r\\n   (or AUTHENTICATE\\r\\n if no pw)
        SIGNAL NEWNYM\\r\\n
        QUIT\\r\\n
    """
    if password:
        # Quote-escape any embedded double-quotes per control-spec.
        escaped = password.replace("\\", "\\\\").replace('"', '\\"')
        auth = f'AUTHENTICATE "{escaped}"\r\n'.encode("utf-8")
    else:
        auth = b"AUTHENTICATE\r\n"
    return [auth, b"SIGNAL NEWNYM\r\n", b"QUIT\r\n"]


async def new_circuit(timeout: float = 5.0) -> bool:
    """Send NEWNYM to the Tor control port to rotate exit IP.

    Returns True on apparent success (got a 250 reply), False on any
    failure (timeout, refused, auth failure, cooldown). Never raises
    — circuit rotation is best-effort; the caller should keep going
    on the existing circuit if rotation fails.

    Cooldown
    --------
    Tor itself rate-limits NEWNYM to once every 10 seconds. We
    enforce a client-side cooldown of the same duration so a 429
    storm doesn't hammer the control port.
    """
    global _last_newnym_ts
    if not is_enabled():
        logger.debug("new_circuit: noop (TOR_PROXY_ENABLED!=1)")
        return False

    loop = asyncio.get_event_loop()
    async with _get_newnym_lock():
        now = loop.time()
        if now - _last_newnym_ts < _NEWNYM_MIN_INTERVAL_S:
            logger.debug(
                "new_circuit: cooldown active (%.1fs since last)",
                now - _last_newnym_ts,
            )
            return False

        host = _control_host()
        port = _control_port()
        password = _control_password()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning("new_circuit: connect failed host=%s port=%d: %s", host, port, exc)
            return False

        try:
            for cmd in format_control_commands(password):
                writer.write(cmd)
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            # Read until QUIT closes the connection — bounded read.
            try:
                response = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            except asyncio.TimeoutError:
                response = b""
            ok = b"250" in response
            if ok:
                _last_newnym_ts = loop.time()
                logger.info("new_circuit: NEWNYM accepted (response=%r)", response[:80])
            else:
                logger.warning("new_circuit: unexpected response: %r", response[:200])
            return ok
        except Exception as exc:  # pragma: no cover - rare I/O race
            logger.warning("new_circuit: I/O error: %s", exc)
            return False
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Health check
# --------------------------------------------------------------------------- #


async def is_healthy(timeout: float = 15.0) -> bool:
    """Verify the Tor circuit by hitting check.torproject.org.

    Returns True iff the response JSON contains ``IsTor=true``. False
    on any failure (proxy down, network timeout, JSON parse error,
    IsTor=false). Never raises.

    Note: when TOR_PROXY_ENABLED!=1 this returns False — we're not
    "healthy as a tor proxy" if we're bypassing tor entirely. Callers
    that want "can I make outbound HTTP at all" should not use this
    function.
    """
    if not is_enabled():
        return False
    client = TorClient(timeout=timeout)
    try:
        resp = await client.get(_TORCHECK_URL)
        if resp.status_code != 200:
            logger.warning("is_healthy: status=%d", resp.status_code)
            return False
        data = resp.json()
        is_tor = bool(data.get("IsTor", False))
        if not is_tor:
            logger.warning("is_healthy: IsTor=false (data=%r)", data)
        return is_tor
    except Exception as exc:
        logger.warning("is_healthy: %s", exc)
        return False
    finally:
        await client.aclose()


__all__ = [
    "DirectClient",
    "TorClient",
    "format_control_commands",
    "get_proxied_client",
    "is_enabled",
    "is_healthy",
    "new_circuit",
]
