"""Fail-closed profile-image intake shared by provider-authorized canaries."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from src.core.vault import write_atomic_artifact


PROFILE_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_IMAGE_TYPES = {
    "image/jpeg": ("jpg", lambda value: value.startswith(b"\xff\xd8\xff")),
    "image/png": ("png", lambda value: value.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/webp": ("webp", lambda value: value.startswith(b"RIFF") and value[8:12] == b"WEBP"),
}


def approved_profile_image_url(url: str, allowed_host_suffixes: tuple[str, ...]) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return bool(
        parsed.scheme == "https"
        and host
        and not parsed.username
        and not parsed.password
        and any(host == suffix or host.endswith("." + suffix) for suffix in allowed_host_suffixes)
    )


def profile_image_extension(content_type: str, data: bytes) -> str | None:
    item = _IMAGE_TYPES.get(content_type.split(";", 1)[0].strip().lower())
    return item[0] if item and item[1](data) else None


async def download_qualified_profile_image(url: str, *, allowed_host_suffixes: tuple[str, ...]) -> tuple[bytes, str]:
    """Download one provider-origin avatar without redirects or type guessing."""
    if not approved_profile_image_url(url, allowed_host_suffixes):
        raise ValueError("profile-image URL is not an approved HTTPS provider origin")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError(f"profile-image provider returned HTTP {response.status_code}")
            declared_size = response.headers.get("content-length")
            if declared_size and int(declared_size) > PROFILE_IMAGE_MAX_BYTES:
                raise ValueError("profile-image exceeds the 5 MiB limit")
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                payload.extend(chunk)
                if len(payload) > PROFILE_IMAGE_MAX_BYTES:
                    raise ValueError("profile-image exceeds the 5 MiB limit")
    extension = profile_image_extension(response.headers.get("content-type", ""), bytes(payload))
    if extension is None:
        raise ValueError("profile-image MIME or magic bytes are not accepted")
    return bytes(payload), extension


def write_qualified_profile_image(*, source: str, artifact_id: str, data: bytes, extension: str,
                                  metadata: dict, root: Path):
    digest = hashlib.sha256(data).hexdigest()
    artifact = write_atomic_artifact(
        source=source,
        artifact_id=artifact_id,
        artifact_kind="media_blob",
        data=data,
        extension=extension,
        expected_sha256=digest,
        metadata=metadata,
        root=root,
    )
    if not artifact.ok or artifact.partial or not artifact.path:
        raise RuntimeError(f"profile-image atomic artifact write failed: {artifact.error}")
    if not artifact.path.is_file() or hashlib.sha256(artifact.path.read_bytes()).hexdigest() != digest:
        raise RuntimeError("profile-image vault file readback failed")
    return artifact, digest
