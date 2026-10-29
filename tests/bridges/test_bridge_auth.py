"""Unit tests for the bridge shared-secret auth + CORS middlewares."""
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from src.bridges.ig_ingest.middleware import (
    bridge_auth_middleware,
    bridge_cors_middleware,
)


def _ok_handler(request):
    async def handler(req):
        return web.json_response({"ok": True})

    return handler


async def _call(middleware, method, path, headers=None):
    request = make_mocked_request(method, path, headers=headers or {})
    return await middleware(request, _ok_handler(request))


async def test_post_without_header_is_401(monkeypatch):
    monkeypatch.setenv("BRIDGE_SHARED_SECRET", "topsecret")
    resp = await _call(bridge_auth_middleware, "POST", "/social/ingest")
    assert resp.status == 401
    assert "secret" in resp.text


async def test_post_with_wrong_header_is_401(monkeypatch):
    monkeypatch.setenv("BRIDGE_SHARED_SECRET", "topsecret")
    resp = await _call(
        bridge_auth_middleware,
        "POST",
        "/social/ingest",
        headers={"X-UC-Bridge-Secret": "nope"},
    )
    assert resp.status == 401


async def test_post_with_correct_header_passes(monkeypatch):
    monkeypatch.setenv("BRIDGE_SHARED_SECRET", "topsecret")
    resp = await _call(
        bridge_auth_middleware,
        "POST",
        "/social/ingest",
        headers={"X-UC-Bridge-Secret": "topsecret"},
    )
    assert resp.status == 200


async def test_health_get_exempt(monkeypatch):
    monkeypatch.setenv("BRIDGE_SHARED_SECRET", "topsecret")
    resp = await _call(bridge_auth_middleware, "GET", "/health")
    assert resp.status == 200


async def test_unset_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("BRIDGE_SHARED_SECRET", raising=False)
    resp = await _call(
        bridge_auth_middleware,
        "POST",
        "/social/ingest",
        headers={"X-UC-Bridge-Secret": ""},
    )
    assert resp.status == 401


async def test_cors_no_allowlist_does_not_reflect(monkeypatch):
    monkeypatch.delenv("BRIDGE_CORS_ALLOWED_ORIGINS", raising=False)
    resp = await _call(
        bridge_cors_middleware,
        "POST",
        "/social/ingest",
        headers={"Origin": "https://evil.example"},
    )
    assert "Access-Control-Allow-Origin" not in resp.headers


async def test_cors_reflects_allowlisted_origin(monkeypatch):
    monkeypatch.setenv("BRIDGE_CORS_ALLOWED_ORIGINS", "https://ok.example")
    resp = await _call(
        bridge_cors_middleware,
        "POST",
        "/social/ingest",
        headers={"Origin": "https://ok.example"},
    )
    assert resp.headers["Access-Control-Allow-Origin"] == "https://ok.example"
    assert resp.headers["Vary"] == "Origin"
