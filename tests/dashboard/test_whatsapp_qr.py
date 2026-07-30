from __future__ import annotations

import asyncio
import json
import os
import urllib.request

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

import src.dashboard.api as dashboard_api
from src.dashboard.api import _should_request_fresh_wa_qr, whatsapp_qr


class _FakeUrlopenResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


def test_should_request_fresh_wa_qr_when_unregistered_and_no_qr():
    assert _should_request_fresh_wa_qr(
        {"whatsapp_ready": False, "registered": False, "status": "disconnected"},
        {"qr": None},
    )


def test_should_request_fresh_wa_qr_while_bridge_is_refreshing_qr():
    assert _should_request_fresh_wa_qr(
        {"whatsapp_ready": False, "registered": False, "status": "refreshing_qr"},
        {"qr": None},
    )


def test_should_not_request_fresh_wa_qr_for_registered_session():
    assert not _should_request_fresh_wa_qr(
        {"whatsapp_ready": False, "registered": True, "status": "disconnected"},
        {"qr": None},
    )


def test_should_not_request_fresh_wa_qr_when_qr_exists():
    assert not _should_request_fresh_wa_qr(
        {"whatsapp_ready": False, "registered": False, "status": "awaiting_scan"},
        {"qr": "raw-code"},
    )


def test_whatsapp_qr_proxy_forwards_bridge_metadata(monkeypatch):
    def fake_urlopen(url: str, timeout: int = 0):
        assert timeout == 8
        if url.endswith("/health"):
            return _FakeUrlopenResponse(
                {
                    "status": "awaiting_scan",
                    "whatsapp_ready": False,
                    "registered": False,
                    "connected": False,
                    "last_disconnect_status_code": None,
                    "last_disconnect_reason": None,
                },
            )
        if url.endswith("/qr"):
            return _FakeUrlopenResponse(
                {
                    "status": "awaiting_scan",
                    "qr": "raw-whatsapp-pairing-code",
                    "qr_available": True,
                    "last_qr_at": "2026-07-28T00:00:00.000Z",
                    "registered": False,
                    "connected": False,
                },
            )
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    out = asyncio.run(whatsapp_qr("1"))

    assert out["status"] == "awaiting_scan"
    assert out["ready"] is False
    assert out["qr_available"] is True
    assert out["last_qr_at"] == "2026-07-28T00:00:00.000Z"
    assert out["registered"] is False
    assert out["connected"] is False
    assert out["qr"].startswith("data:image/png;base64,")


def test_whatsapp_qr_proxy_clears_stale_disconnect_after_fresh_qr_kick(monkeypatch):
    dashboard_api._WA_FRESH_QR_LAST_REQUEST.clear()

    def fake_urlopen(url: str, timeout: int = 0):
        assert timeout == 8
        if url.endswith("/health"):
            return _FakeUrlopenResponse(
                {
                    "status": "disconnected",
                    "whatsapp_ready": False,
                    "registered": False,
                    "connected": False,
                    "last_disconnect_status_code": 408,
                    "last_disconnect_reason": "QR refs attempts ended",
                },
            )
        if url.endswith("/qr"):
            return _FakeUrlopenResponse(
                {
                    "status": "disconnected",
                    "qr": None,
                    "qr_available": False,
                    "last_qr_at": None,
                    "registered": False,
                    "connected": False,
                    "last_disconnect_status_code": 408,
                    "last_disconnect_reason": "QR refs attempts ended",
                },
            )
        raise AssertionError(f"unexpected URL: {url}")

    async def fake_post(bridge: str, path: str):
        return {"bridge": bridge, "ok": True, "status": "fresh_qr_requested"}

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(dashboard_api, "_wa_bridge_post", fake_post)

    out = asyncio.run(whatsapp_qr("2"))

    assert out["status"] == "requesting_fresh_qr"
    assert out["qr_available"] is False
    assert out["last_disconnect_status_code"] is None
    assert out["last_disconnect_reason"] is None


def test_whatsapp_qr_proxy_throttles_repeated_fresh_qr_kicks(monkeypatch):
    dashboard_api._WA_FRESH_QR_LAST_REQUEST["2"] = 1_000_000.0

    def fake_urlopen(url: str, timeout: int = 0):
        assert timeout == 8
        if url.endswith("/health") or url.endswith("/qr"):
            return _FakeUrlopenResponse(
                {
                    "status": "disconnected",
                    "whatsapp_ready": False,
                    "registered": False,
                    "connected": False,
                    "qr": None,
                    "last_disconnect_status_code": 408,
                    "last_disconnect_reason": "QR refs attempts ended",
                },
            )
        raise AssertionError(f"unexpected URL: {url}")

    async def fail_post(_bridge: str, _path: str):
        raise AssertionError("fresh-qr should be throttled")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(dashboard_api.time, "monotonic", lambda: 1_000_010.0)
    monkeypatch.setattr(dashboard_api, "_wa_bridge_post", fail_post)

    out = asyncio.run(whatsapp_qr("2"))

    assert out["status"] == "waiting_for_fresh_qr"
    assert out["last_disconnect_status_code"] is None
    assert out["last_disconnect_reason"] is None

    dashboard_api._WA_FRESH_QR_LAST_REQUEST.clear()


def test_whatsapp_fresh_qr_refuses_registered_bridge(monkeypatch):
    async def fake_get(bridge: str, path: str, timeout: int = 0):
        assert bridge == "1"
        assert path == "health"
        assert timeout == 8
        return {
            "bridge": bridge,
            "ok": True,
            "status": "ready",
            "whatsapp_ready": True,
            "connected": True,
            "registered": True,
        }

    async def fail_post(_bridge: str, _path: str):
        raise AssertionError("fresh-qr must not be proxied for a registered bridge")

    monkeypatch.setattr(dashboard_api, "_wa_bridge_get", fake_get)
    monkeypatch.setattr(dashboard_api, "_wa_bridge_post", fail_post)

    out = asyncio.run(dashboard_api.whatsapp_fresh_qr("1", _user={}))

    assert out["ok"] is True
    assert out["status"] == "registered_session"
    assert out["ready"] is True
    assert out["registered"] is True
