"""Pure URL/username helpers for the lemon8 collector.

Extracted from the collector class (STAGE 2 of the per-package refactor). Pure,
side-effect-free — no ``self``, no I/O (the originals were instance methods but
never touched ``self``). The collector keeps thin instance-method shims so all
internal ``self._foo(...)`` call sites are unchanged.
"""
from __future__ import annotations

import html as html_lib
import re
from typing import Optional


def normalize_username(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    username = value.strip().strip("@").lower()
    username = re.sub(r"[^a-z0-9._]+", "", username)
    return username or None


def clean_media_url(url: str) -> str:
    if not url:
        return ""
    return html_lib.unescape(url)


def is_valid_media_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    if not url.startswith(("http://", "https://")):
        return False
    url_lower = url.lower()
    excluded = [
        ".js", ".css", ".json", ".xml", ".txt", ".html", ".htm",
        "favicon", "logo", "icon", "sprite", "button", "badge",
        "sdk-web", "slardar", "browser.", "_assets/", "static/css",
        "static/js", ".svg", ".woff", ".ttf", ".eot", ".otf",
    ]
    if any(p in url_lower for p in excluded):
        return False
    video_ext = [".mp4", ".webm", ".m4v", ".mov", ".avi", ".flv", ".mkv"]
    image_ext = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"]
    has_ext = any(url_lower.endswith(e) or f"{e}?" in url_lower for e in video_ext + image_ext)
    cdn_pats = [
        "tos-alisg-i-sdweummd6v-sg",
        "tos-alisg-v-a3e477-sg",
        "user-avatar-alisg",
        "/post/",
        "/item/",
        "tplv-sdweummd6v",
    ]
    return has_ext or any(p in url_lower for p in cdn_pats)


def is_small_image(url: str) -> bool:
    if not url:
        return False
    url_lower = url.lower()
    small_indicators = ["thumb", "avatar", "profile_pic", "icon", "favicon", "logo"]
    if any(s in url_lower for s in small_indicators):
        return True

    def _small(w: int, h: int) -> bool:
        dims = [v for v in (w, h) if v > 0]
        return bool(dims) and min(dims) < 250

    m = re.search(r"(\d+)x(\d+)", url_lower)
    if m and _small(int(m.group(1)), int(m.group(2))):
        return True
    m = re.search(r":(\d+):(\d+)", url_lower)
    if m and _small(int(m.group(1)), int(m.group(2))):
        return True
    m = re.search(r"width=(\d+)", url_lower)
    if m and int(m.group(1)) < 250:
        return True
    return False


def is_profile_photo_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(t in u for t in [
        "user-avatar", "avatar", "profile_photo", "profile-photo",
        "profile_pic", "profile-image",
    ])
