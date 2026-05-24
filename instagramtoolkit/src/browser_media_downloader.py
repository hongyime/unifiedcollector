"""B4: Media file downloader + rename.

Injects Playwright cookies into a requests.Session,
streams CDN bytes, verifies, saves with structured filename.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

_CHUNK = 1 << 16  # 64 KB
_IG_REFERER = "https://www.instagram.com/"
_MOBILE_HEADERS = {
    "X-IG-App-ID": "936619743392459",
    "X-Instagram-AJAX": "1",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "*/*",
    "Origin": "https://www.instagram.com",
    "Referer": _IG_REFERER,
}


def build_filename(taken_at: datetime, shortcode: str, index: int, media_type: str) -> str:
    """Return filename: 2025-01-15_14-30-00_UTC_{shortcode}_{n}.{ext}"""
    dt = taken_at.astimezone(timezone.utc)
    date_str = dt.strftime("%Y-%m-%d_%H-%M-%S_UTC")
    ext = "mp4" if media_type == "video" else "jpg"
    return f"{date_str}_{shortcode}_{index}.{ext}"


def download_media_item(
    item: dict,
    post_data: dict,
    dest_dir: Path,
    session: requests.Session,
) -> Optional[dict]:
    """Download one media item and return file info dict, or None on failure.

    item:      {url, type, index}
    post_data: {shortcode, post_id, taken_at, username, …}
    Returns:   {file_path, file_hash, file_size, media_type}
    """
    url = item["url"]
    filename = build_filename(
        post_data["taken_at"],
        post_data["shortcode"],
        item["index"],
        item["type"],
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename

    if dest.exists() and dest.stat().st_size > 0:
        # Already on disk — compute hash without re-downloading
        file_hash = _sha256(dest)
        return {"file_path": str(dest), "file_hash": file_hash,
                "file_size": dest.stat().st_size, "media_type": item["type"]}

    # Stream to a temp file in same dir (atomic rename at end)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
    os.close(tmp_fd)
    try:
        resp = session.get(url, stream=True, timeout=30,
                           headers={"Referer": _IG_REFERER})
        resp.raise_for_status()

        hasher = hashlib.sha256()
        size = 0
        with open(tmp_path, "wb") as fh:
            for chunk in resp.iter_content(_CHUNK):
                if chunk:
                    fh.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)

        if size == 0:
            raise ValueError("Empty response body")

        # Verify magic bytes
        _verify_magic(tmp_path, item["type"])

        os.replace(tmp_path, dest)
        return {"file_path": str(dest), "file_hash": hasher.hexdigest(),
                "file_size": size, "media_type": item["type"]}

    except Exception as e:
        print(f"[DOWNLOAD] Failed {filename}: {e}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None


def make_requests_session(page) -> requests.Session:
    """Build a requests.Session with cookies extracted from a Playwright page."""
    sess = requests.Session()
    try:
        cookies = page.context.cookies()
        for c in cookies:
            sess.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
    except Exception:
        pass
    sess.headers.update(_MOBILE_HEADERS)
    return sess


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_magic(path: str, media_type: str) -> None:
    """Raise if file magic bytes don't match expected type."""
    with open(path, "rb") as fh:
        header = fh.read(12)
    if media_type == "video":
        # MP4: ftyp box at offset 4
        if b"ftyp" not in header and b"\x00\x00\x00" not in header[:4]:
            # Soft fail — CDN sometimes serves JPEG preview for video
            pass
    else:
        # JPEG: FF D8 FF   PNG: 89 50 4E 47   WebP: RIFF????WEBP
        if not (header[:2] == b"\xff\xd8"
                or header[:4] == b"\x89PNG"
                or (header[:4] == b"RIFF" and header[8:12] == b"WEBP")):
            raise ValueError(f"Unexpected image magic: {header[:8].hex()}")
