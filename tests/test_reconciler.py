"""Unit tests for the media Reconciler's pure decision logic.

DB persistence is exercised separately (integration); these cover the bounded /
tombstoning / sharding / alert logic that protects live collection.
"""
import hashlib
import json
import tempfile
from types import SimpleNamespace

import pytest

import httpx
from src.core.reconciler import Reconciler


def _make(source="t", *, present=(), budget=100, tombstone_after=5,
          shards=1, done_after=3):
    """Reconciler with an injected exists() probe and deterministic config."""
    present_set = set(present)
    r = Reconciler(source, exists=lambda p: p in present_set)
    r.enabled = True
    r._done = False
    r.budget = budget
    r.tombstone_after = tombstone_after
    r.shards = shards
    r.done_after = done_after
    return r


def test_present_file_is_not_recovered():
    r = _make(present=["/media/a"])
    r.note_known("a", "/media/a")
    assert r.should_recover("a") is False


def test_missing_file_is_recovered():
    r = _make(present=[])
    r.note_known("a", "/media/a")
    assert r.should_recover("a") is True


def test_unknown_id_not_recovered():
    r = _make()
    assert r.should_recover("never-seen") is False


def test_budget_bounds_releases_per_cycle():
    r = _make(present=[], budget=2)
    for cid in ("a", "b", "c", "d"):
        r.note_known(cid, f"/media/{cid}")
    released = [r.should_recover(c) for c in ("a", "b", "c", "d")]
    assert released == [True, True, False, False]  # only budget=2 released
    # missing_seen counts ALL missing, not just released ones.
    assert r._missing_seen == 4


def test_reset_cycle_refreshes_budget():
    r = _make(present=[], budget=1)
    r.note_known("a", "/media/a")
    r.note_known("b", "/media/b")
    assert r.should_recover("a") is True
    assert r.should_recover("b") is False  # budget spent
    r.reset_cycle()
    assert r.should_recover("b") is True   # budget refreshed next cycle


def test_zero_budget_is_unlimited():
    r = _make(present=[], budget=0)
    for cid in ("a", "b", "c"):
        r.note_known(cid, f"/media/{cid}")
    assert all(r.should_recover(c) for c in ("a", "b", "c"))


def test_tombstone_after_n_failures_then_skipped():
    r = _make(present=[], tombstone_after=3)
    r.note_known("a", "/media/a")
    for _ in range(3):
        assert r.should_recover("a") is True
        r.record_failure("a")
        r.reset_cycle()
    # 3rd failure tombstones it -> never recovered again
    assert "a" in r._tombstoned
    assert r.should_recover("a") is False


def test_inactive_reconciler_recovers_nothing():
    r = _make(present=[])
    r.enabled = False
    r.note_known("a", "/media/a")  # ignored when inactive
    assert r.should_recover("a") is False


def test_done_reconciler_is_inactive():
    r = _make(present=[])
    r._done = True
    assert r.active is False
    r.note_known("a", "/media/a")
    assert r.should_recover("a") is False


def test_missing_rate():
    r = _make(present=[], budget=0)
    for cid in ("a", "b"):
        r.note_known(cid, f"/media/{cid}")
        r.should_recover(cid)
    assert r.missing_rate(4) == 0.5
    assert r.missing_rate(0) == 0.0


def test_sharding_partitions_and_covers_all():
    shards = 4
    ids = [f"id{i}" for i in range(200)]
    seen: set[str] = set()
    for idx in range(shards):
        r = _make(present=[], shards=shards)
        r._shard_index = idx
        for cid in ids:
            r.note_known(cid, f"/media/{cid}")
        shard_ids = set(r._paths)
        # disjoint shards
        assert seen.isdisjoint(shard_ids)
        seen |= shard_ids
    assert seen == set(ids)  # every id covered exactly once across shards


def test_advance_shard_rotates_and_wraps():
    r = _make(shards=3)
    assert r._shard_index == 0
    r.advance_shard(); assert r._shard_index == 1
    r.advance_shard(); assert r._shard_index == 2
    r.advance_shard(); assert r._shard_index == 0  # wraps


def test_sha256_sampling_disabled_by_default():
    r = _make()
    r.sha256_sample_rate = 0
    assert r.sha256_due("anything") is False


def test_sha256_sampling_full_rate_selects_all():
    r = _make()
    r.sha256_sample_rate = 1.0
    assert all(r.sha256_due(f"id{i}") for i in range(50))


def test_file_sha256_matches_hashlib(tmp_path=None):
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"hello reconciler")
        path = f.name
    expected = hashlib.sha256(b"hello reconciler").hexdigest()
    assert Reconciler.file_sha256(path) == expected


def test_file_sha256_missing_returns_none():
    assert Reconciler.file_sha256("/no/such/file/xyz") is None


@pytest.mark.asyncio
async def test_redownload_writes_repair_as_vault_artifact(monkeypatch, tmp_path):
    r = _make("website")
    payload = b"x" * 2048
    seen: dict[str, object] = {}

    class _Response:
        content = payload

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, **kwargs):
            seen["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def get(self, url, **kwargs):
            seen["url"] = url
            seen["headers"] = kwargs.get("headers")
            return _Response()

    def fake_write_atomic_artifact(**kwargs):
        seen["artifact_kwargs"] = kwargs
        return SimpleNamespace(
            ok=True,
            partial=False,
            path=tmp_path / "media" / "blobs" / ("a" * 64) / "x.jpg",
            relative_path="media/blobs/aa/x.jpg",
            blob_relative_path="media/blobs/aa/x.jpg",
            sidecar=SimpleNamespace(relative_path="sidecars/website/x.json"),
            duplicate_blob=False,
            error=None,
            file_size=len(payload),
            sha256="a" * 64,
        )

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr("src.core.reconciler.write_atomic_artifact", fake_write_atomic_artifact)

    repair = await r._redownload(
        "https://cdn.example/image.jpg",
        "/legacy/website/image.jpg",
        content_id="image-1",
    )

    artifact_kwargs = seen["artifact_kwargs"]
    assert artifact_kwargs["source"] == "website"
    assert artifact_kwargs["artifact_id"] == "reconciler/image-1"
    assert artifact_kwargs["artifact_kind"] == "media_blob"
    assert artifact_kwargs["data"] == payload
    assert artifact_kwargs["extension"] == "jpg"
    assert artifact_kwargs["metadata"]["legacy_path"] == "/legacy/website/image.jpg"
    assert repair["file_path"].endswith("x.jpg")
    assert repair["vault_artifact"]["repaired_by"] == "reconciler"


@pytest.mark.asyncio
async def test_update_repaired_media_item_points_row_at_canonical_blob():
    r = _make("website")
    seen: dict[str, object] = {}

    class _Conn:
        async def execute(self, sql, *args):
            seen["sql"] = sql
            seen["args"] = args
            return "UPDATE 1"

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_exc):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    r.pool = _Pool()
    repair = {
        "file_path": "/vault/media/blobs/aa/blob.jpg",
        "file_size": 2048,
        "sha256": "a" * 64,
        "vault_artifact": {
            "path": "media/blobs/aa/blob.jpg",
            "repaired_by": "reconciler",
            "legacy_path": "/legacy/website/image.jpg",
        },
    }

    updated = await r._update_repaired_media_item("image-1", repair)

    assert updated is True
    assert "UPDATE media_items" in seen["sql"]
    assert seen["args"][:5] == (
        "website",
        "image-1",
        "/vault/media/blobs/aa/blob.jpg",
        2048,
        "a" * 64,
    )
    metadata = json.loads(seen["args"][5])
    assert metadata["vault_artifact"]["repaired_by"] == "reconciler"
