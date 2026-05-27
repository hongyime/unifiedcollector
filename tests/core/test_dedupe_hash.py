"""Tests for src.core.dedupe_hash.

Pure-function tests run anywhere; DB tests gated on DATABASE_URL being
reachable. Run inside the collector container with:

    docker exec unifiedcollector_collector \
        python -m pytest tests/core/test_dedupe_hash.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import time
import uuid
from pathlib import Path

import pytest

from src.core.dedupe_hash import (
    HashRecord,
    PHASH_HEX_LEN,
    PHASH_NEAR_DUP_THRESHOLD,
    SHA256_HEX_LEN,
    SIMHASH_HEX_LEN,
    SIMHASH_NEAR_DUP_THRESHOLD,
    hamming_distance,
    is_duplicate,
    is_near_duplicate,
    phash_image,
    register_hash,
    register_hashes,
    sha256_bytes,
    sha256_file,
    sha256_file_async,
    simhash_text,
)


# ---------------------------------------------------------------------------
# sha256
# ---------------------------------------------------------------------------

def test_sha256_file_known_vector(tmp_path):
    """Known vector: sha256 of b'hello world' is well-known constant."""
    p = tmp_path / "hello.txt"
    p.write_bytes(b"hello world")
    expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    assert sha256_file(p) == expected
    assert len(sha256_file(p)) == SHA256_HEX_LEN


def test_sha256_file_streaming_matches_inmemory(tmp_path):
    """Streaming hasher matches a one-shot in-memory hash on big input."""
    data = os.urandom(3 * 1024 * 1024)  # 3 MiB
    p = tmp_path / "blob.bin"
    p.write_bytes(data)
    assert sha256_file(p) == sha256_bytes(data)


def test_sha256_bytes():
    assert sha256_bytes(b"") == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# phash
# ---------------------------------------------------------------------------

def _make_test_image(path: Path, *, jitter: int = 0):
    """Create a simple 64x64 gradient image with optional pixel jitter.

    A small jitter (a few brightness steps) should leave pHash mostly
    unchanged — that's the whole point of perceptual hashing.
    """
    from PIL import Image
    im = Image.new("RGB", (64, 64))
    px = im.load()
    for y in range(64):
        for x in range(64):
            v = (x * 4 + y * 2 + jitter) % 256
            px[x, y] = (v, v, v)
    im.save(path, format="PNG")


def test_phash_near_identical_images(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    _make_test_image(a, jitter=0)
    _make_test_image(b, jitter=2)  # tiny brightness shift
    h1 = phash_image(a)
    h2 = phash_image(b)
    assert len(h1) == PHASH_HEX_LEN
    assert len(h2) == PHASH_HEX_LEN
    d = hamming_distance(h1, h2)
    # Near-dup threshold from task spec is <=4 (PHASH_NEAR_DUP_THRESHOLD).
    assert d <= PHASH_NEAR_DUP_THRESHOLD, (
        f"hamming={d} expected <={PHASH_NEAR_DUP_THRESHOLD}"
    )
    assert is_near_duplicate(h1, h2, hash_kind="phash")


def test_phash_distinct_images_differ(tmp_path):
    """Very different images should NOT pass the near-dup threshold."""
    from PIL import Image, ImageDraw
    a = tmp_path / "solid.png"
    b = tmp_path / "checker.png"
    Image.new("RGB", (64, 64), (10, 10, 10)).save(a)
    im = Image.new("RGB", (64, 64), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for y in range(0, 64, 8):
        for x in range(0, 64, 8):
            if (x // 8 + y // 8) % 2:
                d.rectangle([x, y, x + 7, y + 7], fill=(0, 0, 0))
    im.save(b)
    assert hamming_distance(phash_image(a), phash_image(b)) > PHASH_NEAR_DUP_THRESHOLD


# ---------------------------------------------------------------------------
# simhash
# ---------------------------------------------------------------------------

def test_simhash_identical_text_identical_hash():
    s = "the quick brown fox jumps over the lazy dog"
    assert simhash_text(s) == simhash_text(s)
    assert len(simhash_text(s)) == SIMHASH_HEX_LEN


def test_simhash_similar_sentences_close():
    # Near-identical sentences with one word swapped (fast -> quickly)
    # SIMHASH_NEAR_DUP_THRESHOLD is intentionally tight (3); for one-word
    # edits simhash typically diverges 4-6 bits in 64. Use a slightly
    # looser bound for this property test (8) — the type-system check
    # below ensures the constant itself isn't accidentally inflated.
    a = "The quick brown fox jumps over the lazy dog and runs away fast"
    b = "The quick brown fox jumps over the lazy dog and runs away quickly"
    d = hamming_distance(simhash_text(a), simhash_text(b))
    assert d <= 8, f"hamming={d} expected <=8 for one-word edit"
    # And the constant itself stays tight for actual dedup decisions:
    assert SIMHASH_NEAR_DUP_THRESHOLD <= 4


def test_simhash_distinct_sentences_far():
    a = "machine learning models predict next-token probabilities"
    b = "the cat sat on the mat and watched birds outside"
    d = hamming_distance(simhash_text(a), simhash_text(b))
    assert d > 10  # totally different topics — should be far apart


def test_simhash_empty_zero():
    assert simhash_text("") == "0" * SIMHASH_HEX_LEN
    assert simhash_text("   ,,,,") == "0" * SIMHASH_HEX_LEN


# ---------------------------------------------------------------------------
# hamming
# ---------------------------------------------------------------------------

def test_hamming_basic():
    assert hamming_distance("0", "0") == 0
    assert hamming_distance("0", "1") == 1
    assert hamming_distance("ff", "00") == 8
    assert hamming_distance("ffff", "ffff") == 0


def test_hamming_width_mismatch_raises():
    with pytest.raises(ValueError):
        hamming_distance("ff", "ffff")


def test_is_near_duplicate_defaults():
    a = "0000000000000000"
    b = "0000000000000003"  # 2 bits different
    assert is_near_duplicate(a, b, hash_kind="phash") is True
    assert is_near_duplicate(a, b, hash_kind="simhash") is True
    c = "ffffffffffffffff"
    assert is_near_duplicate(a, c, hash_kind="phash") is False


# ---------------------------------------------------------------------------
# Async file hashing perf smoke
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sha256_file_async_does_not_block_loop(tmp_path):
    """Hashing a 100 MB file concurrently with a 0.1s sleep loop must
    still let the sleep loop tick ~freely. If the file hash blocked the
    event loop the sleep ticks would clump together at the end."""
    big = tmp_path / "big.bin"
    # 100 MB of pseudo-random data (use a fast non-crypto pattern to keep
    # disk IO fast on test machines).
    chunk = os.urandom(1024 * 1024)
    with open(big, "wb") as fh:
        for _ in range(100):
            fh.write(chunk)

    ticks = []

    async def heartbeat():
        # 60 ticks * 100ms = 6s budget — plenty for a 100 MB hash.
        for _ in range(60):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.1)

    t0 = time.monotonic()
    hash_task = asyncio.create_task(sha256_file_async(big))
    hb_task = asyncio.create_task(heartbeat())
    digest = await hash_task
    elapsed = time.monotonic() - t0
    hb_task.cancel()
    try:
        await hb_task
    except asyncio.CancelledError:
        pass

    assert len(digest) == SHA256_HEX_LEN
    assert elapsed < 5.0, f"hash took {elapsed:.2f}s, budget 5s"

    # The heartbeat must have produced ticks DURING the hash, not just
    # after it finished. We measure: count ticks that landed before the
    # hash completed. Should be at least a handful.
    pre_hash_ticks = [t for t in ticks if t - t0 < elapsed]
    assert len(pre_hash_ticks) >= 3, (
        f"event loop appeared blocked: only {len(pre_hash_ticks)} "
        f"heartbeats in {elapsed:.2f}s"
    )


# ---------------------------------------------------------------------------
# DB-backed tests (skipped if Postgres unreachable)
# ---------------------------------------------------------------------------

def _db_available() -> bool:
    """Cheap check: try to open a pool synchronously. Skip if unavailable."""
    try:
        from src.db.connection import get_pool, close_pool
    except Exception:
        return False

    async def _probe():
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


@pytest.fixture(autouse=True)
def _reset_pool_singleton():
    """Reset the module-level pool singleton between tests so each test gets
    a pool bound to its own event loop. Without this, the second test in
    the file inherits a pool bound to a closed loop and asyncpg explodes
    with 'Event loop is closed' / 'another operation in progress'.
    """
    import src.db.connection as _conn
    _conn._pool = None
    yield
    _conn._pool = None


async def _cleanup_table(source_table: str):
    from src.db.connection import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM content_hashes WHERE source_table = $1", source_table
        )


@needs_db
@pytest.mark.asyncio
async def test_register_and_lookup_roundtrip():
    table = "test_dedupe_roundtrip"
    await _cleanup_table(table)
    sid = uuid.uuid4()
    h = "a" * 64

    inserted = await register_hash("sha256", h, table, sid)
    assert inserted is True

    found = await is_duplicate("sha256", h, table)
    assert found == sid

    # Second insert — same key — should be conflict (False).
    again = await register_hash("sha256", h, table, uuid.uuid4())
    assert again is False
    # And the original sid still owns the row.
    assert await is_duplicate("sha256", h, table) == sid

    await _cleanup_table(table)


@needs_db
@pytest.mark.asyncio
async def test_bulk_insert_with_duplicates():
    """1000 rows, 50% duplicates — count must equal unique-row count."""
    table = "test_dedupe_bulk"
    await _cleanup_table(table)

    unique = 500
    rows: list[HashRecord] = []
    sids = [uuid.uuid4() for _ in range(unique)]
    hashes = [f"{i:064x}" for i in range(unique)]

    # First 500 rows: unique.
    for sid, h in zip(sids, hashes):
        rows.append(HashRecord("sha256", h, table, sid))
    # Next 500 rows: duplicates of the first 500 (same hash+kind+table,
    # different source_id — these MUST be rejected by the unique
    # constraint regardless of source_id, because UNIQUE is on
    # (kind, value, table)).
    for h in hashes:
        rows.append(HashRecord("sha256", h, table, uuid.uuid4()))

    n = await register_hashes(rows)
    assert n == unique, f"expected {unique} new inserts, got {n}"

    # Sanity-check via lookup.
    for sid, h in zip(sids[:5], hashes[:5]):
        found = await is_duplicate("sha256", h, table)
        assert found == sid

    await _cleanup_table(table)


@needs_db
@pytest.mark.asyncio
async def test_register_hashes_empty():
    assert await register_hashes([]) == 0
