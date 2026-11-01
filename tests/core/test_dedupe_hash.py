"""Tests for src.core.dedupe_hash (SHA-256 helpers)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time

import pytest

from src.core.dedupe_hash import (
    SHA256_HEX_LEN,
    sha256_bytes,
    sha256_file,
    sha256_file_async,
)


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
    pre_hash_ticks = [t for t in ticks if t - t0 < elapsed]
    min_expected = min(len(ticks), max(3, int((elapsed / 0.1) * 0.5)))
    assert len(pre_hash_ticks) >= min_expected, (
        f"event loop appeared blocked: only {len(pre_hash_ticks)} "
        f"heartbeats concurrent with a {elapsed:.2f}s hash (expected >= {min_expected})"
    )
