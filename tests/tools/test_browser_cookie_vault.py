"""Unit tests for ``src.tools.browser_cookie_vault``.

Runs a real aiohttp server in-process that mimics Chrome's DevTools HTTP +
WebSocket surface for the two calls we exercise (``Storage.getCookies`` /
``Storage.setCookies``). No network, no Chrome.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from src.tools import browser_cookie_vault as vault_mod
from src.tools.browser_cookie_vault import (
    CDPUnreachable,
    CookieVault,
    _sanitize_for_set,
    filter_social_cookies,
)


# ─── Fake CDP server ────────────────────────────────────────────────────────


class FakeChrome:
    """Minimal in-process CDP stand-in.

    - ``/json/version`` returns a payload that points the client at our WS route.
    - ``/ws`` accepts JSON-RPC frames and delegates to ``get_cookies`` /
      ``set_cookies`` handlers driven by test-level fixtures.
    """

    def __init__(self, cookies: list[dict]):
        self.cookies = list(cookies)
        self.set_calls: list[list[dict]] = []
        self.runner: web.AppRunner | None = None
        self.port: int = 0
        self.strict_host: bool = True  # emulate Chrome's DNS-rebinding guard.

    async def _version(self, request: web.Request) -> web.Response:
        if self.strict_host and request.headers.get("Host", "").split(":")[0] not in (
            "localhost",
            "127.0.0.1",
        ):
            return web.Response(status=500, text="Host header validation failed")
        return web.json_response(
            {
                "Browser": "Chrome/test",
                "Protocol-Version": "1.3",
                "webSocketDebuggerUrl": f"ws://localhost:{self.port}/ws",
            }
        )

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data)
            method = frame.get("method")
            msg_id = frame.get("id")
            if method == "Storage.getCookies":
                await ws.send_json({"id": msg_id, "result": {"cookies": self.cookies}})
            elif method == "Storage.setCookies":
                params = frame.get("params") or {}
                self.set_calls.append(list(params.get("cookies") or []))
                await ws.send_json({"id": msg_id, "result": {}})
            else:
                await ws.send_json({"id": msg_id, "result": {}})
        return ws

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/json/version", self._version)
        app.router.add_get("/ws", self._ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        # Grab the port the OS gave us.
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return f"http://localhost:{self.port}"

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()


# ─── Cookie fixtures ────────────────────────────────────────────────────────


def _mk_cookie(name: str, domain: str, **extra) -> dict:
    """Build a cookie shaped like Chrome's ``Storage.getCookies`` output."""
    base = {
        "name": name,
        "value": f"v_{name}",
        "domain": domain,
        "path": "/",
        "expires": time.time() + 86400,
        "size": len(name) * 2,  # read-only; must be stripped on setCookies.
        "session": False,       # read-only; must be stripped on setCookies.
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    }
    base.update(extra)
    return base


def _mixed_jar() -> list[dict]:
    """A representative mix: 8 social cookies + 4 non-social ones."""
    return [
        _mk_cookie("sessionid", ".instagram.com"),
        _mk_cookie("ds_user_id", ".instagram.com"),
        _mk_cookie("csrftoken", ".instagram.com"),
        _mk_cookie("sessionid", ".tiktok.com"),
        _mk_cookie("c_user", ".facebook.com"),
        _mk_cookie("auth_token", ".x.com"),
        _mk_cookie("token", ".threads.com"),
        _mk_cookie("_strava4_session", ".strava.com"),
        # Non-social — must be dropped by the filter.
        _mk_cookie("SID", ".google.com"),
        _mk_cookie("session", ".claude.ai"),
        _mk_cookie("cf_clearance", ".cloudflare.com"),
        _mk_cookie("track", ".example.com"),
    ]


# ─── Domain-filter tests ────────────────────────────────────────────────────


def test_filter_drops_non_social_cookies():
    filtered = filter_social_cookies(_mixed_jar())
    domains = {c["domain"] for c in filtered}
    # Every non-social domain gone.
    assert ".google.com" not in domains
    assert ".claude.ai" not in domains
    assert ".cloudflare.com" not in domains
    assert ".example.com" not in domains
    # Every social cookie retained.
    assert len(filtered) == 8
    assert domains == {
        ".instagram.com",
        ".tiktok.com",
        ".facebook.com",
        ".x.com",
        ".threads.com",
        ".strava.com",
    }


def test_filter_accepts_cdn_suffixes():
    jar = [
        _mk_cookie("cdn", ".fbcdn.net"),
        _mk_cookie("cdn", ".cdninstagram.com"),
        _mk_cookie("cdn", "static.twimg.com"),
        _mk_cookie("nope", ".example.org"),
    ]
    filtered = filter_social_cookies(jar)
    assert {c["domain"] for c in filtered} == {
        ".fbcdn.net",
        ".cdninstagram.com",
        "static.twimg.com",
    }


def test_sanitize_strips_readonly_fields():
    c = _mk_cookie("sessionid", ".instagram.com")
    clean = _sanitize_for_set(c)
    assert "size" not in clean
    assert "session" not in clean
    # Legitimate fields kept.
    assert clean["name"] == "sessionid"
    assert clean["domain"] == ".instagram.com"
    assert clean["sameSite"] == "None"


def test_sanitize_drops_invalid_samesite():
    c = _mk_cookie("x", ".x.com", sameSite="unset")
    assert "sameSite" not in _sanitize_for_set(c)


# ─── Backup / restore round-trip via fake Chrome ────────────────────────────


async def test_backup_writes_latest_json_with_expected_shape(tmp_path: Path):
    fake = FakeChrome(_mixed_jar())
    cdp_url = await fake.start()
    try:
        vault = CookieVault(cdp_url=cdp_url, backup_dir=tmp_path, keep_snapshots=10)
        async with aiohttp.ClientSession() as session:
            payload = await vault.backup(session)
    finally:
        await fake.stop()

    latest = tmp_path / "latest.json"
    assert latest.is_file()
    disk = json.loads(latest.read_text(encoding="utf-8"))
    # Payload shape contract.
    assert disk["cookie_count"] == 8
    assert disk["total_seen"] == 12
    assert disk["cdp_url"] == cdp_url
    assert isinstance(disk["ts"], str) and disk["ts"].endswith("Z")
    assert len(disk["cookies"]) == 8
    # And a timestamped snapshot is written alongside.
    snapshots = list(tmp_path.glob("snapshot_*.json"))
    assert len(snapshots) == 1
    # In-memory state reflects the write.
    assert vault.last_backup_count == 8
    assert vault.last_error is None
    assert payload == disk


async def test_restore_pushes_each_cookie_via_set_cookies(tmp_path: Path):
    fake = FakeChrome(cookies=[])  # cookies-on-Chrome doesn't matter for restore.
    cdp_url = await fake.start()
    try:
        # Seed a snapshot on disk (representative jar).
        snapshot = {
            "ts": "2026-01-01T00:00:00Z",
            "cdp_url": cdp_url,
            "cookie_count": 3,
            "total_seen": 3,
            "cookies": [
                _mk_cookie("sessionid", ".instagram.com"),
                _mk_cookie("c_user", ".facebook.com"),
                _mk_cookie("auth_token", ".x.com"),
            ],
        }
        (tmp_path / "latest.json").write_text(json.dumps(snapshot), encoding="utf-8")

        vault = CookieVault(cdp_url=cdp_url, backup_dir=tmp_path)
        async with aiohttp.ClientSession() as session:
            counts = await vault.restore(session)
    finally:
        await fake.stop()

    # Every cookie triggered exactly one Storage.setCookies call.
    assert len(fake.set_calls) == 3
    # Read-only fields must not be forwarded.
    for call in fake.set_calls:
        assert len(call) == 1
        cookie = call[0]
        assert "size" not in cookie
        assert "session" not in cookie
    # Per-domain counts returned to the caller.
    assert counts == {"instagram.com": 1, "facebook.com": 1, "x.com": 1}


async def test_snapshot_pruning_keeps_only_configured_window(tmp_path: Path):
    fake = FakeChrome(_mixed_jar())
    cdp_url = await fake.start()
    try:
        vault = CookieVault(cdp_url=cdp_url, backup_dir=tmp_path, keep_snapshots=3)
        # Seed 5 stale snapshots with sortable names.
        for stamp in ("20250101T000000Z", "20250102T000000Z", "20250103T000000Z",
                      "20250104T000000Z", "20250105T000000Z"):
            (tmp_path / f"snapshot_{stamp}.json").write_text("{}", encoding="utf-8")
        async with aiohttp.ClientSession() as session:
            await vault.backup(session)
    finally:
        await fake.stop()

    snapshots = sorted(p.name for p in tmp_path.glob("snapshot_*.json"))
    # keep_snapshots=3 -> only 3 files survive, and they're the newest by name.
    assert len(snapshots) == 3
    # The freshly-written snapshot (this run) must be among them.
    assert any(name != f"snapshot_{stamp}.json" for name in snapshots for stamp in
               ("20250101T000000Z", "20250102T000000Z"))
    # The two oldest are gone.
    assert "snapshot_20250101T000000Z.json" not in snapshots
    assert "snapshot_20250102T000000Z.json" not in snapshots


# ─── Chrome-down retry / backoff ────────────────────────────────────────────


async def test_cdp_unreachable_when_no_server(tmp_path: Path):
    """No listener on the port -> connect() raises CDPUnreachable."""
    vault = CookieVault(cdp_url="http://127.0.0.1:1", backup_dir=tmp_path)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(CDPUnreachable):
            await vault.backup(session)


async def test_daemon_loop_backs_off_when_chrome_missing(tmp_path: Path, monkeypatch):
    """The main loop must exponentially back off and NOT tight-loop."""
    vault = CookieVault(cdp_url="http://127.0.0.1:1", backup_dir=tmp_path)

    async def fail_backup(_session):
        raise CDPUnreachable("simulated")

    monkeypatch.setattr(vault, "backup", fail_backup)

    slept: list[float] = []

    async def fake_wait_for(coro, timeout):
        slept.append(timeout)
        # Close the coroutine we're pretending to wait on so no warnings leak.
        coro.close()
        # After 3 backoff sleeps, trip the stop event to end the loop.
        if len(slept) >= 3:
            stop_event.set()
        raise asyncio.TimeoutError

    stop_event = asyncio.Event()
    monkeypatch.setattr(vault_mod.asyncio, "wait_for", fake_wait_for)

    await vault_mod._backup_loop(vault, interval=300, stop_event=stop_event)

    # Backoff must grow: 5 -> 10 -> 20 (mult=2, min=5).
    assert slept[:3] == pytest.approx([5.0, 10.0, 20.0])
    assert vault.consecutive_failures >= 3
    assert vault.last_error and vault.last_error.startswith("cdp_unreachable")


def test_cdp_unreachable_warning_is_periodic(monkeypatch):
    monkeypatch.setattr(vault_mod, "CDP_UNREACHABLE_WARN_EVERY", 12)

    assert vault_mod._should_warn_cdp_unreachable(1) is True
    assert vault_mod._should_warn_cdp_unreachable(2) is False
    assert vault_mod._should_warn_cdp_unreachable(11) is False
    assert vault_mod._should_warn_cdp_unreachable(12) is True
    assert vault_mod._should_warn_cdp_unreachable(24) is True


# ─── Health endpoint ────────────────────────────────────────────────────────


async def test_health_endpoint_reports_last_backup(tmp_path: Path, aiohttp_unused_port_factory=None):
    """The /health JSON reflects vault state after a successful backup."""
    fake = FakeChrome(_mixed_jar())
    cdp_url = await fake.start()
    try:
        vault = CookieVault(cdp_url=cdp_url, backup_dir=tmp_path)
        async with aiohttp.ClientSession() as session:
            await vault.backup(session)

        app = vault_mod._build_health_app(vault)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                    data = await resp.json()
        finally:
            await runner.cleanup()
    finally:
        await fake.stop()

    assert data["ok"] is True
    assert data["count"] == 8
    assert data["error"] is None
    assert data["last_backup"] is not None
    assert data["cdp_url"] == cdp_url
