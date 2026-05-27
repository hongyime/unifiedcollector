"""Unified content hashing + deduplication for all collectors.

Replaces ~800 LOC of duplicated hashing logic across github, instagram,
lemon8, telegram, and tiktok toolkits with a single canonical module.

Three hash kinds:
    sha256_file(path)     -> hex string  (exact-dupe content hash)
    phash_image(path)     -> hex string  (perceptual 64-bit hash)
    simhash_text(text)    -> hex string  (text 64-bit simhash)

All hashes are stored as lower-case hex strings of fixed width:
    sha256: 64 chars
    phash:  16 chars (64 bits)
    simhash:16 chars (64 bits)

Dedup index lives in Postgres table `content_hashes`:
    UNIQUE (hash_kind, hash_value, source_table)
indexed on (hash_kind, hash_value) and (source_table, source_id).

Hamming distance helpers compare two hex hashes of equal width.
Default near-dup thresholds: pHash <=4, simhash <=3.

Async-friendliness:
    sha256_file streams in 1 MiB chunks. Files >10 MiB are dispatched to
    a thread (asyncio.to_thread) via sha256_file_async() so an event loop
    is not blocked. The synchronous sha256_file remains available for
    callers running outside async contexts.

Not for cryptographic auth — content fingerprinting only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional
from uuid import UUID

logger = logging.getLogger(__name__)


# -- Tunables -----------------------------------------------------------------

SHA256_HEX_LEN = 64
PHASH_HEX_LEN = 16   # 64 bits / 4 bits per hex char
SIMHASH_HEX_LEN = 16

# Thread-pool threshold for async file hashing.
THREAD_OFFLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB

# Recommended near-dup thresholds (callers may override).
PHASH_NEAR_DUP_THRESHOLD = 4
SIMHASH_NEAR_DUP_THRESHOLD = 3


# -- HashRecord ---------------------------------------------------------------

@dataclass(slots=True)
class HashRecord:
    """One row destined for content_hashes."""

    hash_kind: str
    hash_value: str
    source_table: str
    source_id: UUID


# -- File hashing -------------------------------------------------------------

def sha256_file(path: str | os.PathLike, *, chunk_size: int = 1 << 20) -> str:
    """Stream a file through sha256, return lowercase hex digest.

    Streams in chunks (default 1 MiB) so memory usage stays flat
    regardless of file size. Use sha256_file_async() inside async
    code to avoid blocking the event loop on large files.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


async def sha256_file_async(path: str | os.PathLike) -> str:
    """Async-friendly file hasher.

    For files >= 10 MiB the work runs in a default-executor thread so
    the event loop stays responsive. Smaller files run inline because
    the syscall overhead of dispatching to a thread dominates.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if size >= THREAD_OFFLOAD_BYTES:
        return await asyncio.to_thread(sha256_file, path)
    return sha256_file(path)


def sha256_bytes(data: bytes) -> str:
    """SHA-256 of an in-memory bytes object."""
    return hashlib.sha256(data).hexdigest()


# -- Perceptual hash for images -----------------------------------------------

def phash_image(path: str | os.PathLike) -> str:
    """Compute 64-bit perceptual hash of an image, return 16-char hex.

    Uses the imagehash library's pHash (DCT-based). Two near-identical
    images normally differ by <4 hamming bits (PHASH_NEAR_DUP_THRESHOLD).
    """
    import imagehash  # local import: heavy, optional at module-import time
    from PIL import Image

    with Image.open(path) as im:
        # imagehash works with any PIL mode; convert to a stable mode so
        # pHash is invariant across e.g. RGBA vs RGB on identical content.
        if im.mode not in ("L", "RGB"):
            im = im.convert("RGB")
        ph = imagehash.phash(im, hash_size=8)  # 8x8 = 64 bits
    return _imagehash_to_hex(ph)


async def phash_image_async(path: str | os.PathLike) -> str:
    """Run pHash in a thread — image decode + DCT can take 50ms+ per image."""
    return await asyncio.to_thread(phash_image, path)


def _imagehash_to_hex(ph) -> str:
    """imagehash.ImageHash -> 16-char lowercase hex (64 bits)."""
    # ImageHash.__str__ already gives hex but width can be unpadded.
    s = str(ph)
    # Defensive: pad / truncate to 16 chars.
    if len(s) < PHASH_HEX_LEN:
        s = s.rjust(PHASH_HEX_LEN, "0")
    elif len(s) > PHASH_HEX_LEN:
        s = s[-PHASH_HEX_LEN:]
    return s.lower()


# -- Text simhash -------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens used as simhash features."""
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text)]


def simhash_text(text: str) -> str:
    """Compute 64-bit simhash of arbitrary text, return 16-char hex.

    Pure-Python implementation (no extra dep). Two captions/comments
    differing by 1-2 word edits typically differ by <3 hamming bits
    (SIMHASH_NEAR_DUP_THRESHOLD). Empty input returns all zeros.
    """
    if not text:
        return "0" * SIMHASH_HEX_LEN

    tokens = _tokenize(text)
    if not tokens:
        return "0" * SIMHASH_HEX_LEN

    # Weight = token frequency.
    freq: dict[str, int] = {}
    for tok in tokens:
        freq[tok] = freq.get(tok, 0) + 1

    bits = [0] * 64
    for tok, weight in freq.items():
        # Use sha256 truncated to 64 bits as the per-token hash. md5 would
        # also work but sha256 keeps the dependency surface symmetric with
        # sha256_file and is plenty fast at this granularity.
        h = int.from_bytes(hashlib.sha256(tok.encode("utf-8")).digest()[:8], "big")
        for i in range(64):
            if (h >> i) & 1:
                bits[i] += weight
            else:
                bits[i] -= weight

    out = 0
    for i, b in enumerate(bits):
        if b > 0:
            out |= (1 << i)

    return f"{out:016x}"


# -- Hamming distance ---------------------------------------------------------

def hamming_distance(h1: str, h2: str) -> int:
    """Hamming distance between two hex hashes of equal length.

    Raises ValueError if the inputs are different widths — that almost
    always means the caller mixed hash kinds (e.g. sha256 vs phash) and
    the comparison would be meaningless.
    """
    if len(h1) != len(h2):
        raise ValueError(
            f"hamming_distance: width mismatch {len(h1)} vs {len(h2)} "
            f"(comparing different hash kinds?)"
        )
    a = int(h1, 16)
    b = int(h2, 16)
    return (a ^ b).bit_count()


def is_near_duplicate(
    h1: str,
    h2: str,
    *,
    threshold: int | None = None,
    hash_kind: str = "phash",
) -> bool:
    """Convenience: hamming_distance <= threshold.

    If threshold is None, use the default for the given hash_kind.
    """
    if threshold is None:
        if hash_kind == "phash":
            threshold = PHASH_NEAR_DUP_THRESHOLD
        elif hash_kind == "simhash":
            threshold = SIMHASH_NEAR_DUP_THRESHOLD
        else:
            # sha256 has no meaningful near-dup notion — bit-flips are random.
            threshold = 0
    return hamming_distance(h1, h2) <= threshold


# -- Postgres-backed dedup index ----------------------------------------------

_VALID_HASH_KINDS = {"sha256", "phash", "simhash"}


def _validate_kind(hash_kind: str) -> None:
    if hash_kind not in _VALID_HASH_KINDS:
        raise ValueError(
            f"unknown hash_kind {hash_kind!r}; "
            f"expected one of {sorted(_VALID_HASH_KINDS)}"
        )


async def is_duplicate(
    hash_kind: str,
    hash_value: str,
    source_table: str,
    *,
    pool=None,
) -> Optional[UUID]:
    """Return existing source_id if (kind, value, table) already seen, else None.

    pool: an asyncpg.Pool. If omitted, falls back to db.connection.get_pool().
    """
    _validate_kind(hash_kind)
    if pool is None:
        from src.db.connection import get_pool
        pool = await get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT source_id
              FROM content_hashes
             WHERE hash_kind = $1
               AND hash_value = $2
               AND source_table = $3
             LIMIT 1
            """,
            hash_kind, hash_value.lower(), source_table,
        )
    return row["source_id"] if row else None


async def find_near_duplicates(
    hash_kind: str,
    hash_value: str,
    source_table: str,
    *,
    threshold: int | None = None,
    limit: int = 50,
    pool=None,
) -> list[tuple[UUID, str, int]]:
    """Scan content_hashes for fuzzy matches.

    Returns up to `limit` (source_id, hash_value, distance) tuples with
    distance <= threshold. NOT indexed — O(N) over rows of (kind, table).
    Use sparingly, e.g. on insert paths only.

    Only meaningful for phash / simhash. For sha256 a near-dup search is
    pointless (any single-bit flip means totally different content), so
    we hard-coded threshold=0 for sha256 — equivalent to is_duplicate.
    """
    _validate_kind(hash_kind)
    if hash_kind == "sha256":
        existing = await is_duplicate(hash_kind, hash_value, source_table, pool=pool)
        if existing is not None:
            return [(existing, hash_value.lower(), 0)]
        return []

    if threshold is None:
        threshold = (
            PHASH_NEAR_DUP_THRESHOLD if hash_kind == "phash"
            else SIMHASH_NEAR_DUP_THRESHOLD
        )

    if pool is None:
        from src.db.connection import get_pool
        pool = await get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source_id, hash_value
              FROM content_hashes
             WHERE hash_kind = $1
               AND source_table = $2
            """,
            hash_kind, source_table,
        )

    out: list[tuple[UUID, str, int]] = []
    target = hash_value.lower()
    target_int = int(target, 16)
    width_bits = len(target) * 4
    for row in rows:
        candidate = row["hash_value"]
        if len(candidate) * 4 != width_bits:
            continue  # malformed row; skip
        d = (target_int ^ int(candidate, 16)).bit_count()
        if d <= threshold:
            out.append((row["source_id"], candidate, d))
            if len(out) >= limit:
                break
    out.sort(key=lambda t: t[2])
    return out


async def register_hash(
    hash_kind: str,
    hash_value: str,
    source_table: str,
    source_id: UUID,
    *,
    pool=None,
) -> bool:
    """Insert one hash row. Returns True if new, False if conflict.

    ON CONFLICT DO NOTHING semantics — safe to call repeatedly.
    """
    _validate_kind(hash_kind)
    if pool is None:
        from src.db.connection import get_pool
        pool = await get_pool()

    async with pool.acquire() as conn:
        # asyncpg's `execute` returns a status string like 'INSERT 0 1' or
        # 'INSERT 0 0'. We parse the trailing count.
        status = await conn.execute(
            """
            INSERT INTO content_hashes (hash_kind, hash_value, source_table, source_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (hash_kind, hash_value, source_table) DO NOTHING
            """,
            hash_kind, hash_value.lower(), source_table, source_id,
        )
    try:
        inserted = int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        inserted = 0
    return inserted == 1


async def register_hashes(
    rows: Iterable[HashRecord],
    *,
    pool=None,
) -> int:
    """Bulk insert hash rows with ON CONFLICT DO NOTHING.

    Returns the count actually inserted (i.e. minus conflicts). Uses a
    temp table + INSERT ... SELECT so we get an accurate insert count
    despite DO NOTHING (executemany would not give us per-row status).
    """
    rows = list(rows)
    if not rows:
        return 0

    for r in rows:
        _validate_kind(r.hash_kind)

    if pool is None:
        from src.db.connection import get_pool
        pool = await get_pool()

    payload = [
        (r.hash_kind, r.hash_value.lower(), r.source_table, r.source_id)
        for r in rows
    ]

    async with pool.acquire() as conn:
        async with conn.transaction():
            # asyncpg's copy_records_to_table is much faster than executemany
            # for thousands of rows. We stage into a TEMP table, then upsert.
            await conn.execute(
                """
                CREATE TEMP TABLE _content_hashes_stage
                  (LIKE content_hashes INCLUDING DEFAULTS)
                  ON COMMIT DROP
                """
            )
            await conn.copy_records_to_table(
                "_content_hashes_stage",
                records=payload,
                columns=["hash_kind", "hash_value", "source_table", "source_id"],
            )
            inserted_row = await conn.fetchrow(
                """
                WITH ins AS (
                    INSERT INTO content_hashes
                        (hash_kind, hash_value, source_table, source_id)
                    SELECT hash_kind, hash_value, source_table, source_id
                      FROM _content_hashes_stage
                    ON CONFLICT (hash_kind, hash_value, source_table) DO NOTHING
                    RETURNING 1
                )
                SELECT COUNT(*) AS n FROM ins
                """
            )
    return int(inserted_row["n"]) if inserted_row else 0


# -- Module exports -----------------------------------------------------------

__all__ = [
    "HashRecord",
    "PHASH_HEX_LEN",
    "PHASH_NEAR_DUP_THRESHOLD",
    "SHA256_HEX_LEN",
    "SIMHASH_HEX_LEN",
    "SIMHASH_NEAR_DUP_THRESHOLD",
    "find_near_duplicates",
    "hamming_distance",
    "is_duplicate",
    "is_near_duplicate",
    "phash_image",
    "phash_image_async",
    "register_hash",
    "register_hashes",
    "sha256_bytes",
    "sha256_file",
    "sha256_file_async",
    "simhash_text",
]
