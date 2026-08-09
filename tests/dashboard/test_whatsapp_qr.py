from __future__ import annotations

import asyncio
import json
import os
import urllib.request

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

import src.dashboard.api as dashboard_api
from src.dashboard.api import _should_wait_for_fresh_wa_qr, whatsapp_qr


class _FakeUrlopenResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


def test_should_wait_for_fresh_wa_qr_when_unregistered_and_no_qr():
    assert _should_wait_for_fresh_wa_qr(
        {"whatsapp_ready": False, "registered": False, "status": "disconnected"},
        {"qr": None},
    )


def test_should_wait_for_fresh_wa_qr_while_bridge_is_refreshing_qr():
    assert _should_wait_for_fresh_wa_qr(
        {"whatsapp_ready": False, "registered": False, "status": "refreshing_qr"},
        {"qr": None},
    )


def test_should_not_request_fresh_wa_qr_for_registered_session():
    assert not _should_wait_for_fresh_wa_qr(
        {"whatsapp_ready": False, "registered": True, "status": "disconnected"},
        {"qr": None},
    )


def test_should_not_request_fresh_wa_qr_when_qr_exists():
    assert not _should_wait_for_fresh_wa_qr(
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
                    "last_disconnect_at": None,
                    "pairing_recovery_until": None,
                    "pairing_recovery_active": False,
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
                    "last_disconnect_at": "2026-07-28T00:00:01.000Z",
                    "pairing_recovery_until": "2026-07-28T00:01:31.000Z",
                    "pairing_recovery_active": True,
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
    assert out["last_disconnect_at"] == "2026-07-28T00:00:01.000Z"
    assert out["pairing_recovery_until"] == "2026-07-28T00:01:31.000Z"
    assert out["pairing_recovery_active"] is True
    assert out["qr"].startswith("data:image/png;base64,")


def test_whatsapp_qr_proxy_waits_without_destructive_fresh_qr_post(monkeypatch):
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
        raise AssertionError(f"QR polling must not POST {bridge}/{path}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(dashboard_api, "_wa_bridge_post", fake_post)

    out = asyncio.run(whatsapp_qr("2"))

    assert out["status"] == "waiting_for_fresh_qr"
    assert out["qr_available"] is False
    assert out["last_disconnect_status_code"] is None
    assert out["last_disconnect_reason"] is None
    assert "without clearing WhatsApp auth state" in out["error"]


def test_whatsapp_qr_proxy_waits_while_refreshing_qr_without_post(monkeypatch):
    def fake_urlopen(url: str, timeout: int = 0):
        assert timeout == 8
        if url.endswith("/health") or url.endswith("/qr"):
            return _FakeUrlopenResponse(
                {
                    "status": "refreshing_qr",
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
        raise AssertionError("fresh-qr must not be posted from QR polling")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(dashboard_api, "_wa_bridge_post", fail_post)

    out = asyncio.run(whatsapp_qr("2"))

    assert out["status"] == "waiting_for_fresh_qr"
    assert out["last_disconnect_status_code"] is None
    assert out["last_disconnect_reason"] is None


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


def test_whatsapp_pairing_code_rejects_invalid_phone(monkeypatch):
    async def fake_get(bridge: str, path: str, timeout: int = 0):
        return {
            "bridge": bridge,
            "ok": True,
            "status": "awaiting_scan",
            "whatsapp_ready": False,
            "connected": False,
            "registered": False,
        }

    async def fail_post(_bridge: str, _path: str, _payload=None):
        raise AssertionError("invalid phone must not be sent to bridge")

    class FakeRequest:
        async def json(self):
            return {"phone": "not-a-phone"}

    monkeypatch.setattr(dashboard_api, "_wa_bridge_get", fake_get)
    monkeypatch.setattr(dashboard_api, "_wa_bridge_post", fail_post)

    out = asyncio.run(dashboard_api.whatsapp_pairing_code("1", FakeRequest(), _user={}))

    assert out["ok"] is False
    assert out["status"] == "invalid_phone"


def test_whatsapp_pairing_code_proxies_phone_to_unregistered_bridge(monkeypatch):
    async def fake_get(bridge: str, path: str, timeout: int = 0):
        assert bridge == "2"
        assert path == "health"
        assert timeout == 8
        return {
            "bridge": bridge,
            "ok": True,
            "status": "awaiting_scan",
            "whatsapp_ready": False,
            "connected": False,
            "registered": False,
        }

    async def fake_post(bridge: str, path: str, payload=None):
        assert bridge == "2"
        assert path == "pairing-code"
        assert payload == {"phone": "00000000"}
        return {"bridge": bridge, "ok": True, "status": "pairing_code_requested", "code": "ABCD-1234"}

    class FakeRequest:
        async def json(self):
            return {"phone": "00000000"}

    monkeypatch.setattr(dashboard_api, "_wa_bridge_get", fake_get)
    monkeypatch.setattr(dashboard_api, "_wa_bridge_post", fake_post)

    out = asyncio.run(dashboard_api.whatsapp_pairing_code("2", FakeRequest(), _user={}))

    assert out["ok"] is True
    assert out["code"] == "ABCD-1234"
    assert out["phone_last4"] == "8112"
