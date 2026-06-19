"""Unit tests for the media Reconciler's pure decision logic.

DB persistence is exercised separately (integration); these cover the bounded /
tombstoning / sharding / alert logic that protects live collection.
"""
import hashlib
import tempfile

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
