#!/usr/bin/env python3
"""
Shared media classification rules for download features.

This keeps the standalone downloader and the unified processor aligned so they
both accept only supported media:
- native photos
- document-backed images
- videos
- round video notes

Everything else, including office documents, archives, audio, animations, and
WebM files, is rejected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Tuple

from telethon.tl.types import DocumentAttributeAnimated, DocumentAttributeVideo


def classify_document_media(doc: Any) -> Optional[Tuple[str, str]]:
    """Return supported media type/extension for a document, or None."""
    mime_type = (getattr(doc, "mime_type", "") or "").lower()
    attributes = getattr(doc, "attributes", []) or []

    file_extension = _infer_extension_from_attributes(attributes)
    is_video = False
    is_video_note = False
    is_animated = False

    for attr in attributes:
        if isinstance(attr, DocumentAttributeVideo):
            is_video = True
            if getattr(attr, "round_message", False):
                is_video_note = True
        elif isinstance(attr, DocumentAttributeAnimated):
            is_animated = True

    if is_animated:
        return None

    if mime_type.startswith("audio/"):
        return None

    if is_video_note:
        return "videonote", "mp4"

    if is_video or mime_type.startswith("video/"):
        if "webm" in mime_type or file_extension == "webm":
            return None
        return "video", _normalize_video_extension(file_extension, mime_type)

    if mime_type.startswith("image/"):
        return "image", _normalize_image_extension(file_extension, mime_type)

    return None


def _infer_extension_from_attributes(attributes: list[Any]) -> str:
    """Infer a file extension from document attributes when available."""
    for attr in attributes:
        file_name = getattr(attr, "file_name", "")
        if not file_name:
            continue
        suffix = Path(file_name).suffix.lstrip(".").lower()
        if suffix:
            return suffix
    return ""


def _normalize_video_extension(file_extension: str, mime_type: str) -> str:
    """Normalize video extensions to a safe supported subset."""
    if file_extension in {"mp4", "mov", "avi", "mkv"}:
        return file_extension
    if "quicktime" in mime_type or "mov" in mime_type:
        return "mov"
    if "avi" in mime_type:
        return "avi"
    if "x-matroska" in mime_type or "mkv" in mime_type:
        return "mkv"
    return "mp4"


def _normalize_image_extension(file_extension: str, mime_type: str) -> str:
    """Normalize image extensions to common safe formats."""
    if file_extension in {"jpg", "jpeg", "png", "webp", "gif"}:
        return "jpg" if file_extension == "jpeg" else file_extension
    if "png" in mime_type:
        return "png"
    if "webp" in mime_type:
        return "webp"
    if "gif" in mime_type:
        return "gif"
    return "jpg"
