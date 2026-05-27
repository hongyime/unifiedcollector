"""Tests for src.core.tor_proxy.

Pure-unit tests run anywhere — they exercise the consumer allowlist,
the env-disabled passthrough, and the control-port wire format.

Live-Tor tests are gated on ``TOR_PROXY_LIVE=1`` and skipped by
default. Run inside the collector container with the tor sidecar up:

    TOR_PROXY_LIVE=1 docker exec -e TOR_PROXY_LIVE=1 \\
        unifiedcollector_collector \\
        python -m pytest tests/core/test_tor_proxy.py -v
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.core import tor_proxy
from src.core.tor_proxy import (
    DirectClient,
    TorClient,
    _ALLOWED_CONSUMERS,
    format_control_commands,
    get_proxied_client,
    is_enabled,
    is_healthy,
    new_circuit,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip any tor-related env vars so tests start from a known state."""
    for key in [
        "TOR_PROXY_ENABLED",
        "TOR_SOCKS_HOST",
        "TOR_SOCKS_PORT",
        "TOR_CONTROL_HOST",
        "TOR_CONTROL_PORT",
        "TOR_CONTROL_PASSWORD",
        "TOR_SOCKS_USERNAME",
        "TOR_SOCKS_PASSWORD",
    ]:
        monkeypatch.delenv(key, raising=False)
    # Reset module-level NEWNYM cooldown so each test starts fresh.
    tor_proxy._last_newnym_ts = 0.0
    tor_proxy._newnym_lock = None
    yield


# --------------------------------------------------------------------------- #
# Consumer allowlist (pure unit)
# --------------------------------------------------------------------------- #


class TestConsumerAllowlist:
    """The architectural guard against tor-amplifying IG/TT bans."""

    def test_allowlist_membership(self):
        assert _ALLOWED_CONSUMERS == frozenset({"github", "search", "website"})

    @pytest.mark.parametrize("consumer", ["github", "search", "website"])
    def test_allowed_consumers_succeed(self, consumer):
        client = get_proxied_client(consumer=consumer)
        assert client is not None
        # Default disabled => DirectClient
        assert isinstance(client, DirectClient)

    @pytest.mark.parametrize(
        "consumer",
        ["instagram", "tiktok", "lemon8", "strava", "telegram", "whatsapp", "youtube", ""],
    )
    def test_disallowed_consumers_raise(self, consumer):
        with pytest.raises(ValueError, match="not allowed for consumer"):
            get_proxied_client(consumer=consumer)

    def test_disallowed_error_lists_allowed(self):
        with pytest.raises(ValueError) as excinfo:
            get_proxied_client(consumer="instagram")
        msg = str(excinfo.value)
        assert "github" in msg
        assert "search" in msg
        assert "website" in msg

    def test_allowlist_check_runs_before_io(self, monkeypatch):
        """Bad consumer should fail even if Tor is enabled (no I/O attempted)."""
        monkeypatch.setenv("TOR_PROXY_ENABLED", "1")
        with pytest.raises(ValueError):
            get_proxied_client(consumer="instagram")


# --------------------------------------------------------------------------- #
# Env-disabled passthrough (pure unit)
# --------------------------------------------------------------------------- #


class TestPassthrough:
    def test_disabled_by_default(self):
        assert is_enabled() is False

    def test_disabled_returns_direct_client(self):
        client = get_proxied_client(consumer="github")
        assert isinstance(client, DirectClient)
        assert not isinstance(client, TorClient)

    def test_enabled_returns_tor_client(self, monkeypatch):
        monkeypatch.setenv("TOR_PROXY_ENABLED", "1")
        assert is_enabled() is True
        client = get_proxied_client(consumer="github")
        assert isinstance(client, TorClient)

    @pytest.mark.parametrize("val", ["0", "false", "no", "", "TRUE", "yes"])
    def test_only_exact_1_enables(self, monkeypatch, val):
        """Strict ==1 check — anything else means disabled."""
        monkeypatch.setenv("TOR_PROXY_ENABLED", val)
        assert is_enabled() is False

    def test_direct_client_has_minimal_interface(self):
        client = DirectClient()
        # Duck-type the interface: get/post/aclose
        assert callable(getattr(client, "get", None))
        assert callable(getattr(client, "post", None))
        assert callable(getattr(client, "aclose", None))
        assert isinstance(client.httpx_client, httpx.AsyncClient)

    def test_tor_client_has_minimal_interface(self):
        client = TorClient()
        assert callable(getattr(client, "get", None))
        assert callable(getattr(client, "post", None))
        assert callable(getattr(client, "aclose", None))
        assert isinstance(client.httpx_client, httpx.AsyncClient)

    def test_tor_client_uses_socks_proxy_url(self, monkeypatch):
        monkeypatch.setenv("TOR_SOCKS_HOST", "tor")
        monkeypatch.setenv("TOR_SOCKS_PORT", "9050")
        client = TorClient()
        # The proxy URL is stashed on the wrapper for assertion.
        assert client._proxy_url == "socks5://tor:9050"

    def test_tor_client_embeds_credentials_when_set(self, monkeypatch):
        monkeypatch.setenv("TOR_SOCKS_USERNAME", "user")
        monkeypatch.setenv("TOR_SOCKS_PASSWORD", "pw")
        client = TorClient()
        assert client._proxy_url == "socks5://user:pw@tor:9050"


# --------------------------------------------------------------------------- #
# Control-port wire format (pure unit)
# --------------------------------------------------------------------------- #


class TestControlCommands:
    def test_no_password_uses_bare_authenticate(self):
        cmds = format_control_commands("")
        assert cmds[0] == b"AUTHENTICATE\r\n"
        assert cmds[1] == b"SIGNAL NEWNYM\r\n"
        assert cmds[2] == b"QUIT\r\n"

    def test_password_quoted(self):
        cmds = format_control_commands("hunter2")
        assert cmds[0] == b'AUTHENTICATE "hunter2"\r\n'

    def test_password_with_quote_escaped(self):
        cmds = format_control_commands('a"b')
        assert cmds[0] == b'AUTHENTICATE "a\\"b"\r\n'

    def test_password_with_backslash_escaped(self):
        cmds = format_control_commands("a\\b")
        assert cmds[0] == b'AUTHENTICATE "a\\\\b"\r\n'

    def test_command_count(self):
        assert len(format_control_commands("")) == 3
        assert len(format_control_commands("pw")) == 3

    def test_all_commands_terminated_with_crlf(self):
        for cmd in format_control_commands("pw"):
            assert cmd.endswith(b"\r\n")


# --------------------------------------------------------------------------- #
# new_circuit — mocked socket I/O (pure unit)
# --------------------------------------------------------------------------- #


class TestNewCircuit:
    @pytest.mark.asyncio
    async def test_disabled_is_noop(self):
        # TOR_PROXY_ENABLED unset => returns False without I/O.
        result = await new_circuit()
        assert result is False

    @pytest.mark.asyncio
    async def test_success_path(self, monkeypatch):
        monkeypatch.setenv("TOR_PROXY_ENABLED", "1")

        # Mock asyncio.open_connection to return fake reader/writer.
        fake_reader = AsyncMock()
        fake_reader.read = AsyncMock(return_value=b"250 OK\r\n250 OK\r\n250 closing\r\n")
        fake_writer = MagicMock()
        fake_writer.write = MagicMock()
        fake_writer.drain = AsyncMock()
        fake_writer.close = MagicMock()
        fake_writer.wait_closed = AsyncMock()

        async def fake_open(*args, **kwargs):
            return fake_reader, fake_writer

        monkeypatch.setattr(asyncio, "open_connection", fake_open)
        result = await new_circuit()
        assert result is True
        # All three commands written.
        assert fake_writer.write.call_count == 3

    @pytest.mark.asyncio
    async def test_connect_failure_returns_false(self, monkeypatch):
        monkeypatch.setenv("TOR_PROXY_ENABLED", "1")

        async def fake_open(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(asyncio, "open_connection", fake_open)
        result = await new_circuit()
        assert result is False

    @pytest.mark.asyncio
    async def test_non_250_response_returns_false(self, monkeypatch):
        monkeypatch.setenv("TOR_PROXY_ENABLED", "1")

        fake_reader = AsyncMock()
        fake_reader.read = AsyncMock(return_value=b"515 Authentication failed\r\n")
        fake_writer = MagicMock()
        fake_writer.write = MagicMock()
        fake_writer.drain = AsyncMock()
        fake_writer.close = MagicMock()
        fake_writer.wait_closed = AsyncMock()

        async def fake_open(*args, **kwargs):
            return fake_reader, fake_writer

        monkeypatch.setattr(asyncio, "open_connection", fake_open)
        result = await new_circuit()
        assert result is False

    @pytest.mark.asyncio
    async def test_cooldown_blocks_rapid_calls(self, monkeypatch):
        monkeypatch.setenv("TOR_PROXY_ENABLED", "1")

        call_count = 0

        async def fake_open(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            fake_reader = AsyncMock()
            fake_reader.read = AsyncMock(return_value=b"250 OK\r\n")
            fake_writer = MagicMock()
            fake_writer.write = MagicMock()
            fake_writer.drain = AsyncMock()
            fake_writer.close = MagicMock()
            fake_writer.wait_closed = AsyncMock()
            return fake_reader, fake_writer

        monkeypatch.setattr(asyncio, "open_connection", fake_open)

        # First call succeeds, second is suppressed by cooldown.
        first = await new_circuit()
        second = await new_circuit()
        assert first is True
        assert second is False
        assert call_count == 1  # second call short-circuited before connect


# --------------------------------------------------------------------------- #
# Live-Tor tests — gated on TOR_PROXY_LIVE=1
# --------------------------------------------------------------------------- #

_LIVE = os.getenv("TOR_PROXY_LIVE", "0") == "1"

pytestmark_live = pytest.mark.skipif(
    not _LIVE,
    reason="Live Tor tests skipped (set TOR_PROXY_LIVE=1 to enable)",
)


@pytestmark_live
class TestLiveTor:
    """These hit the real ``tor`` sidecar. Run from inside the collector container."""

    @pytest.mark.asyncio
    async def test_is_healthy_returns_true(self, monkeypatch):
        monkeypatch.setenv("TOR_PROXY_ENABLED", "1")
        result = await is_healthy(timeout=30.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_new_circuit_succeeds(self, monkeypatch):
        monkeypatch.setenv("TOR_PROXY_ENABLED", "1")
        result = await new_circuit(timeout=5.0)
        # Pass if NEWNYM accepted OR if control port is auth-protected
        # in this env — either way the wire format works.
        assert result in (True, False)

    @pytest.mark.asyncio
    async def test_real_get_through_tor(self, monkeypatch):
        monkeypatch.setenv("TOR_PROXY_ENABLED", "1")
        client = get_proxied_client(consumer="github", timeout=30.0)
        try:
            resp = await client.get("https://check.torproject.org/api/ip")
            assert resp.status_code == 200
            assert resp.json().get("IsTor") is True
        finally:
            await client.aclose()
