"""Shared media validation gate — keep ONLY real PDF / image / video / audio,
drop favicons, tracking pixels, sprite sheets, thumbnails, and HTML error pages
that masquerade as media.

Two checks, both cheap and dependency-free:
  1. MAGIC-BYTE SNIFF — the bytes must actually start with a known media
     signature. An HTML login wall saved as ".jpg" fails here. This also gives us
     the TRUE type/extension regardless of what the URL or caller claimed.
  2. MINIMUM SIZE per type — favicons/thumbnails are tiny; a real photo/video is
     not. Tunable via env (MEDIA_MIN_IMAGE_BYTES, ...).

Usage (download-time gate):
    ok, kind, mtype, reason = inspect(data, content_type_header)
    if not ok: skip
    # kind = "jpg"/"png"/"mp4"/"pdf"...   mtype = "image"/"video"/"pdf"/"audio"

Allowed top-level types default to the user's set: PDF, IMAGE, VIDEO (+ AUDIO,
which the messaging collectors legitimately keep). Override with
MEDIA_ALLOWED_TYPES="image,video,pdf".
"""
from __future__ import annotations

import os

# Per-type minimum bytes. Favicons are ~1-5KB; a real photo is tens of KB+.
MIN_BYTES = {
    "image": int(os.getenv("MEDIA_MIN_IMAGE_BYTES", "20000")),   # 20 KB -> kills favicons/thumbs
    "video": int(os.getenv("MEDIA_MIN_VIDEO_BYTES", "100000")),  # 100 KB
    "pdf":   int(os.getenv("MEDIA_MIN_PDF_BYTES", "5000")),      # 5 KB
    "audio": int(os.getenv("MEDIA_MIN_AUDIO_BYTES", "8000")),    # 8 KB
}

ALLOWED_TYPES = {
    t.strip().lower()
    for t in os.getenv("MEDIA_ALLOWED_TYPES", "image,video,pdf,audio").split(",")
    if t.strip()
}

_IMAGE = {"jpg", "png", "gif", "webp", "bmp", "tiff", "heic"}
_VIDEO = {"mp4", "webm", "mov", "avi", "mkv"}
_AUDIO = {"mp3", "ogg", "wav", "flac", "m4a"}


def sniff(data: bytes) -> str | None:
    """Return a media kind from magic bytes, or None if not recognized."""
    if len(data) < 12:
        return None
    b = data
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    if b[:4] == b"RIFF" and b[8:12] == b"AVI ":
        return "avi"
    if b[:2] == b"BM":
        return "bmp"
    if b[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if b[:4] == b"%PDF":
        return "pdf"
    if b[4:8] == b"ftyp":
        # mp4/mov/heic family — disambiguate by brand
        brand = b[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heif", b"mif1"):
            return "heic"
        if brand in (b"qt  ",):
            return "mov"
        return "mp4"
    if b[:4] == b"\x1aE\xdf\xa3":
        return "webm"  # Matroska/WebM
    if b[:3] == b"ID3" or b[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if b[:4] == b"OggS":
        return "ogg"
    if b[:4] == b"fLaC":
        return "flac"
    if b[:4] == b"RIFF" and b[8:12] == b"WAVE":
        return "wav"
    return None


def kind_to_type(kind: str | None) -> str | None:
    if kind in _IMAGE:
        return "image"
    if kind in _VIDEO:
        return "video"
    if kind in _AUDIO:
        return "audio"
    if kind == "pdf":
        return "pdf"
    return None


def looks_like_html(data: bytes) -> bool:
    head = data[:512].lstrip()[:64].lower()
    return head.startswith(b"<") or b"<!doctype" in head or b"<html" in head


def inspect(data: bytes, content_type_header: str | None = None):
    """Validate downloaded bytes.

    Returns (ok: bool, kind: str|None, mtype: str|None, reason: str).
    `kind` is the true extension (jpg/png/mp4/pdf...), `mtype` the top-level
    category (image/video/pdf/audio). Reject HTML pages, unrecognized bytes,
    disallowed types, and anything under the per-type minimum size.
    """
    if not data:
        return False, None, None, "empty"
    if looks_like_html(data):
        return False, None, None, "html/error page"
    kind = sniff(data)
    if kind is None:
        ct = (content_type_header or "").split(";")[0].strip().lower()
        if ct == "application/pdf":
            kind = "pdf"
        else:
            return False, None, None, f"unrecognized non-media bytes (ct={ct or '?'})"
    mtype = kind_to_type(kind)
    if mtype not in ALLOWED_TYPES:
        return False, kind, mtype, f"type '{mtype or kind}' not allowed"
    minb = MIN_BYTES.get(mtype, 5000)
    if len(data) < minb:
        return False, kind, mtype, f"too small ({len(data)}B < {minb}B {mtype} min — likely favicon/thumbnail)"
    return True, kind, mtype, "ok"


__all__ = ["inspect", "sniff", "kind_to_type", "looks_like_html", "MIN_BYTES", "ALLOWED_TYPES"]
