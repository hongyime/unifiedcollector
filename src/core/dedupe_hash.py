"""Content hashing helpers shared by collectors.

Canonical SHA-256 fingerprinting used for exact-duplicate detection:

    sha256_file(path)          -> hex string (streaming, 1 MiB chunks)
    sha256_file_async(path)    -> hex string (thread-offloaded for large files)
    sha256_bytes(data)         -> hex string (in-memory bytes)

Not for cryptographic auth — content fingerprinting only.
"""

from __future__ import annotations

import asyncio
import hashlib
import os

SHA256_HEX_LEN = 64

# Thread-pool threshold for async file hashing.
THREAD_OFFLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB


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


__all__ = [
    "SHA256_HEX_LEN",
    "sha256_bytes",
    "sha256_file",
    "sha256_file_async",
]
