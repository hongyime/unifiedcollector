"""Outcome-state tests for the ig_ingest bridge (task 27, GAP-4).

The four ingestion states must stay mutually exclusive in every payload:

- queued:   a background task was scheduled; nothing durable happened yet.
- accepted: the ingest was acknowledged (queued items, or a dedupe hit).
- stored:   a durable save completed (`stored > 0` implies `saved > 0`).
- deduped:  a duplicate_content_id/duplicate_sha256 hit; never counted as stored.

A background-task failure must surface as a failed browser_ingest_event, never
as an accepted-as-stored payload.
"""
import asyncio
import importlib
import json

import pytest

from src.bridges import ig_ingest

# The package re-exports the `ingest` handler, shadowing the submodule
# attribute; import the route module explicitly.
ingest_routes = importlib.import_module("src.bridges.ig_ingest.ingest")


class _FakeConn:
    def __init__(self):
        self.executes = []
        self.executemany_calls = []

    async def execute(self, query, *args):
        self.executes.append((query, args))
        return "INSERT 0 1"

    async def executemany(self, query, args):
        self.executemany_calls.append((query, list(args)))


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return _AcquireContext(self.conn)


class _FakeRequest(dict):
    def __init__(self, app, body):
        super().__init__()
        self.app = app
        self._body = body
        self.headers = {}

    async def json(self):
        return self._body


def _upload_body(**overrides):
    body = {
        "platform": "instagram",
        "username": "alice",
        "extension_version": "1.27.0",
        "file_size": 22000,
        "mime_type": "image/jpeg",
        "item": {
            "content_id": "story_abc123",
            "url": "https://scontent.cdninstagram.com/v/t51.29350-15/abc.jpg",
        },
    }
    body.update(overrides)
    return body


def test_social_ingest_response_is_queued_not_stored(monkeypatch):
    drained = []

    async def fake_drain(_app, platform, username, items, _extension_version, _surface):
        drained.append((platform, username, [item["content_id"] for item in items]))

    monkeypatch.setattr(ig_ingest, "_drain", fake_drain)
    app = {"pool": _FakePool(), "tasks": set()}

    async def run():
        resp = await ingest_routes.ingest(_FakeRequest(app, {
            "platform": "instagram",
            "username": "timeline",
            "items": [{"content_id": "m1"}, {"content_id": "m2"}],
        }))
        await asyncio.gather(*app["tasks"])
        return json.loads(resp.text)

    payload = asyncio.run(run())

    assert payload == {"accepted": 2, "queued": True, "stored": 0, "platform": "instagram"}
    assert drained == [("instagram", "timeline", ["m1", "m2"])]


def test_social_ingest_empty_and_verified_responses_never_claim_queued_or_stored():
    app = {"pool": _FakePool(), "tasks": set()}

    empty = asyncio.run(ingest_routes.ingest(_FakeRequest(app, {
        "platform": "instagram",
        "username": "timeline",
        "items": [],
    })))
    verified = asyncio.run(ingest_routes.ingest(_FakeRequest(app, {
        "platform": "instagram",
        "username": "badge_target",
        "items": [],
        "is_verified": True,
    })))

    assert json.loads(empty.text) == {
        "accepted": 0,
        "queued": False,
        "stored": 0,
        "platform": "instagram",
    }
    assert json.loads(verified.text) == {
        "accepted": 0,
        "queued": False,
        "stored": 0,
        "platform": "instagram",
        "excluded_verified": True,
    }
    assert app["tasks"] == set() or all(task.done() for task in app["tasks"])


def test_upload_route_response_is_queued_with_stored_zero(monkeypatch):
    scheduled = []

    def fake_schedule(_app, coro, label):
        scheduled.append((label, coro))
        coro.close()

    monkeypatch.setattr(ig_ingest, "_schedule_app_task", fake_schedule)
    app = {"pool": _FakePool()}

    resp = asyncio.run(ingest_routes.ingest_upload(
        _FakeRequest(app, _upload_body()),
    ))

    payload = json.loads(resp.text)
    assert payload == {
        "accepted": 1,
        "stored": 0,
        "saved": 0,
        "deduped": False,
        "queued": True,
        "platform": "instagram",
        "content_id": "story_abc123",
    }
    assert scheduled[0][0] == "browser_upload_ingest"


def _run_upload(monkeypatch, download_impl):
    pool = _FakePool()
    events = []
    candidates = []

    async def fake_candidate(_pool, platform, username, item, **kwargs):
        candidates.append((platform, username, item.get("content_id"), kwargs.get("saved")))

    async def fake_event(_pool, platform, endpoint, subject, **kwargs):
        events.append((platform, endpoint, subject, kwargs))

    monkeypatch.setattr(ig_ingest, "_download_and_save", download_impl)
    monkeypatch.setattr(ig_ingest, "_record_browser_media_candidate", fake_candidate)
    monkeypatch.setattr(ig_ingest, "_record_browser_ingest_event", fake_event)

    app = {
        "pool": pool,
        "session": object(),
        "sem": asyncio.Semaphore(1),
    }
    result = asyncio.run(ig_ingest._ingest_uploaded_media(app, "instagram", _upload_body()))
    return result, events, candidates


def test_upload_stored_reports_stored_only_after_durable_save(monkeypatch):
    async def fake_download(_pool, _session, _platform, _username, _item, reject_stats):
        return True

    result, events, candidates = _run_upload(monkeypatch, fake_download)

    assert result == {
        "accepted": 1,
        "stored": 1,
        "saved": 1,
        "deduped": False,
        "failed": False,
        "platform": "instagram",
        "reason": None,
        "reject_stats": {},
    }
    assert candidates == [("instagram", "alice", "story_abc123", True)]
    assert len(events) == 1
    platform, endpoint, subject, kwargs = events[0]
    assert (platform, endpoint, subject) == ("instagram", "media", "alice")
    assert kwargs["observed_count"] == 1
    assert kwargs["stored_count"] == 1
    assert kwargs["deduped_count"] == 0
    assert kwargs["failed_count"] == 0
    assert kwargs["outcome"] == "success"
    assert kwargs["error_class"] is None
    assert kwargs["metadata"]["accepted"] is True
    assert kwargs["metadata"]["saved"] is True
    assert kwargs["metadata"]["deduped"] is False
    assert kwargs["metadata"]["failed"] is False


def test_upload_dedupe_is_own_state_and_never_stored(monkeypatch):
    async def fake_download(_pool, _session, _platform, _username, _item, reject_stats):
        reject_stats["duplicate_content_id"] = 1
        return False

    result, events, candidates = _run_upload(monkeypatch, fake_download)

    assert result["accepted"] == 1
    assert result["stored"] == 0
    assert result["saved"] == 0
    assert result["deduped"] is True
    assert result["failed"] is False
    assert candidates == [("instagram", "alice", "story_abc123", False)]
    assert len(events) == 1
    kwargs = events[0][3]
    assert kwargs["stored_count"] == 0
    assert kwargs["deduped_count"] == 1
    assert kwargs["failed_count"] == 0
    assert kwargs["outcome"] == "success"
    assert kwargs["metadata"]["accepted"] is True
    assert kwargs["metadata"]["saved"] is False
    assert kwargs["metadata"]["deduped"] is True


def test_upload_failure_records_failed_event_not_accepted(monkeypatch):
    async def fake_download(_pool, _session, _platform, _username, _item, reject_stats):
        raise RuntimeError("vault_unavailable")

    result, events, candidates = _run_upload(monkeypatch, fake_download)

    assert result["accepted"] == 0
    assert result["stored"] == 0
    assert result["saved"] == 0
    assert result["deduped"] is False
    assert result["failed"] is True
    assert result["reason"] == "RuntimeError"
    assert candidates == [("instagram", "alice", "story_abc123", False)]
    assert len(events) == 1
    platform, endpoint, subject, kwargs = events[0]
    assert (platform, endpoint, subject) == ("instagram", "media", "alice")
    assert kwargs["observed_count"] == 1
    assert kwargs["stored_count"] == 0
    assert kwargs["deduped_count"] == 0
    assert kwargs["failed_count"] == 1
    assert kwargs["outcome"] == "failed"
    assert kwargs["error_class"] == "RuntimeError"
    assert kwargs["metadata"]["accepted"] is False
    assert kwargs["metadata"]["failed"] is True
    assert kwargs["metadata"]["error_class"] == "RuntimeError"


def test_upload_rejection_is_failed_without_stored_or_accepted(monkeypatch):
    async def fake_download(_pool, _session, _platform, _username, _item, reject_stats):
        reject_stats["unsupported_mime"] = 1
        return False

    result, events, _candidates = _run_upload(monkeypatch, fake_download)

    assert result["accepted"] == 0
    assert result["stored"] == 0
    assert result["failed"] is True
    assert result["deduped"] is False
    assert result["reason"] == "unsupported_mime"
    kwargs = events[0][3]
    assert kwargs["stored_count"] == 0
    assert kwargs["failed_count"] == 1
    assert kwargs["outcome"] == "failed"
    assert kwargs["metadata"]["accepted"] is False


@pytest.mark.parametrize("body", [
    _upload_body(),
    _upload_body(data_b64="aGVsbG8=", url="https://example.test/direct.jpg"),
])
def test_upload_route_queued_response_covers_plain_and_direct_url_bodies(monkeypatch, body):
    scheduled = []

    def fake_schedule(_app, coro, label):
        scheduled.append((label, coro))
        coro.close()

    monkeypatch.setattr(ig_ingest, "_schedule_app_task", fake_schedule)

    resp = asyncio.run(ingest_routes.ingest_upload(_FakeRequest({"pool": _FakePool()}, body)))

    payload = json.loads(resp.text)
    assert payload["queued"] is True
    assert payload["stored"] == 0
    assert payload["accepted"] == 1
    assert scheduled[0][0] == "browser_upload_ingest"
