from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


SERVICE_ROOT = Path(__file__).resolve().parents[2] / "services" / "bulk_sender"
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))


def load_module(name: str):
    return importlib.import_module(f"bulk_sender.{name}")


def test_external_hourly_cap_has_hard_floor_of_30():
    sender_mod = load_module("sender")
    assert sender_mod.effective_external_hourly_cap(5) == 5
    assert sender_mod.effective_external_hourly_cap(30) == 30
    assert sender_mod.effective_external_hourly_cap(999) == 30


def test_file_hash_dedup_is_stable(tmp_path):
    sender_mod = load_module("sender")
    file_path = tmp_path / "sample.bin"
    file_path.write_bytes(b"abc123")

    digest1 = sender_mod.file_sha256(str(file_path))
    digest2 = sender_mod.file_sha256(str(file_path))
    assert digest1 == digest2
    assert len(digest1) == 64


def test_internal_target_is_config_locked(monkeypatch):
    cfg_mod = load_module("config")
    monkeypatch.setattr(cfg_mod.settings, "BULK_SENDER_INTERNAL_TARGET_JID", "findings@g.us")
    assert cfg_mod.settings.BULK_SENDER_INTERNAL_TARGET_JID == "findings@g.us"


@pytest.mark.asyncio
async def test_send_media_requires_bridge_secret(monkeypatch):
    sender_mod = load_module("sender")
    cfg_mod = load_module("config")

    monkeypatch.setattr(cfg_mod.settings, "MEDIA_BRIDGE_SECRET", "")
    sender = sender_mod.BulkSender()

    result = await sender.send_media("session_1", "123@g.us", "/data/media/x.bin")
    assert result.sent is False
    assert result.reason == "media_bridge_secret_missing"


@pytest.mark.asyncio
async def test_send_media_posts_to_bridge(monkeypatch):
    sender_mod = load_module("sender")
    cfg_mod = load_module("config")

    monkeypatch.setattr(cfg_mod.settings, "MEDIA_BRIDGE_SECRET", "x" * 32)
    monkeypatch.setattr(cfg_mod.settings, "MEDIA_BRIDGE_URL", "http://wa-client-ts-1:3001")
    monkeypatch.setattr(cfg_mod.settings, "SESSION_BRIDGES_JSON", "")

    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"message_id": "wamid.abc123"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, content, headers):
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(sender_mod.httpx, "AsyncClient", lambda *args, **kwargs: FakeClient())
    sender = sender_mod.BulkSender()

    result = await sender.send_media("session_1", "12345@g.us", "/data/media/sample.bin")
    assert result.sent is True
    assert result.wa_message_id == "wamid.abc123"
    assert captured["url"].endswith("/send-media")
    assert captured["headers"]["X-Signature"]
