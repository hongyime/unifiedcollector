"""Pure URL-classification helpers for the search collector.

Extracted from the collector class (STAGE 2 of the per-package refactor). Pure,
side-effect-free — no ``self``, no I/O. The collector keeps a staticmethod shim
delegating here, and re-imports CONTENT_EXTENSIONS / ICON_KEYWORDS for back-compat.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

CONTENT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".jfif", ".pdf"}

ICON_KEYWORDS = {
    "icon", "logo", "favicon", "sprite", "thumb", "avatar", "badge",
    "button", "arrow", "spacer", "pixel", "tracking", "analytics",
}


def is_content_url(url: str) -> bool:
    """Filter URLs to plausible image/PDF assets, skipping icons/sprites."""
    url_lower = url.lower()
    path = urlparse(url_lower).path
    ext = os.path.splitext(path)[1]
    if ext not in CONTENT_EXTENSIONS:
        return False
    for kw in ICON_KEYWORDS:
        if kw in path:
            return False
    return True
