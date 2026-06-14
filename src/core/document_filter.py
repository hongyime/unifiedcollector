"""Tier-3 document & sticker classification (COLLECTION_SPEC).

Shared building block (Phase 0) used by any collector that ingests arbitrary
file attachments — telegram, whatsapp, beeper.

Spec (Tier 3 — Documents & audio):
  * Documents: **whitelist safe types only** — PDF, Word, PowerPoint, Office,
    images, text. **Skip executables and code files.**
  * Audio: **store the file** (no transcription here).
  * Stickers: **collect static, skip animated** (.tgs / animated .webm).

The classifier is intentionally conservative: an *unknown* type is treated as
NOT safe (skipped) so we never pull an executable just because its MIME was
unfamiliar. Callers pass whatever signal they have (MIME and/or filename); the
decision uses the extension first (most reliable), then the MIME family.
"""
from __future__ import annotations

import os
from typing import NamedTuple, Optional

# ── safe document extensions (Tier 3 whitelist) ─────────────────────────────
# PDF, Word, PowerPoint, Excel/Office, OpenDocument, plain text/markdown,
# common images, ebooks, archives of documents, subtitles.
SAFE_DOC_EXTENSIONS: frozenset[str] = frozenset({
    # PDF / office
    "pdf", "doc", "docx", "dot", "dotx", "rtf",
    "xls", "xlsx", "xlsm", "csv", "tsv",
    "ppt", "pptx", "pps", "ppsx",
    "odt", "ods", "odp", "odg",  # OpenDocument
    "pages", "numbers", "key",   # Apple iWork
    # text
    "txt", "md", "markdown", "log", "rtf", "vcf", "ics",
    # images (also Tier 2, but documents can carry them)
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif", "heic", "svg",
    # ebooks
    "epub", "mobi", "azw3", "djvu",
    # subtitles
    "srt", "vtt", "ass", "sub",
})

# ── blocked: executables, scripts, code, installers (Tier 3 "skip") ─────────
BLOCKED_DOC_EXTENSIONS: frozenset[str] = frozenset({
    # native executables / libraries / installers
    "exe", "dll", "so", "dylib", "bin", "msi", "app", "apk", "ipa", "deb",
    "rpm", "dmg", "pkg", "appimage", "com", "scr", "sys", "drv",
    # shell / batch
    "sh", "bash", "zsh", "bat", "cmd", "ps1", "psm1", "vbs", "wsf",
    # code (source)
    "py", "pyc", "pyo", "pyw", "js", "mjs", "cjs", "ts", "tsx", "jsx",
    "c", "h", "cpp", "cc", "cxx", "hpp", "java", "class", "jar", "kt",
    "rb", "go", "rs", "php", "pl", "pm", "lua", "swift", "scala", "clj",
    "cs", "vb", "fs", "r", "m", "mm", "asm", "s",
    # web/code-ish that can carry payloads
    "html", "htm", "xhtml", "jar", "war", "ear",
})


class DocDecision(NamedTuple):
    """Result of classifying a document/sticker attachment."""
    download: bool
    content_type: str   # 'document' | 'audio' | 'video' | 'sticker' | 'image'
    reason: str         # human-readable, for debug logging


def _ext_from(mime: Optional[str], filename: Optional[str]) -> str:
    """Best-effort lowercase extension (no dot) from filename then MIME."""
    if filename:
        base = filename.strip().lower()
        if "." in os.path.basename(base):
            return base.rsplit(".", 1)[-1][:12]
    if mime:
        clean = mime.split(";")[0].strip().lower()
        # a few MIME→ext shortcuts the stdlib misses
        shortcuts = {
            "application/pdf": "pdf",
            "application/msword": "doc",
            "application/vnd.ms-excel": "xls",
            "application/vnd.ms-powerpoint": "ppt",
            "image/webp": "webp",
            "image/jpeg": "jpg",
            "text/plain": "txt",
            "application/x-tgsticker": "tgs",
        }
        if clean in shortcuts:
            return shortcuts[clean]
        if "officedocument.wordprocessing" in clean:
            return "docx"
        if "officedocument.spreadsheet" in clean:
            return "xlsx"
        if "officedocument.presentation" in clean:
            return "pptx"
        if "/" in clean:
            return clean.rsplit("/", 1)[-1][:12]
    return ""


def classify_document(
    mime: Optional[str],
    filename: Optional[str] = None,
    *,
    is_sticker: bool = False,
    is_animated: bool = False,
    is_audio: bool = False,
    is_video: bool = False,
) -> DocDecision:
    """Decide whether to download an attachment and under which content_type.

    `is_sticker` / `is_animated` / `is_audio` / `is_video` are optional platform
    signals (e.g. Telethon DocumentAttribute*). When unavailable they default to
    False and the decision falls back to MIME/extension inspection.
    """
    mime_l = (mime or "").split(";")[0].strip().lower()
    ext = _ext_from(mime, filename)

    # ── stickers: static keep, animated skip ───────────────────────────────
    sticker_like = is_sticker or ext == "tgs" or mime_l == "application/x-tgsticker"
    if sticker_like:
        animated = (
            is_animated
            or ext == "tgs"
            or mime_l in {"application/x-tgsticker", "video/webm"}
        )
        if animated:
            return DocDecision(False, "sticker", f"animated sticker skipped ({ext or mime_l})")
        return DocDecision(True, "sticker", "static sticker")

    # ── audio: always store the file (Tier 3) ──────────────────────────────
    if is_audio or mime_l.startswith("audio/"):
        return DocDecision(True, "audio", "audio file")

    # ── video: Tier 2 media, always full ───────────────────────────────────
    if is_video or mime_l.startswith("video/"):
        # animated webm stickers are caught above; a plain video is fine.
        return DocDecision(True, "video", "video file")

    # ── images embedded as documents ───────────────────────────────────────
    if mime_l.startswith("image/") or ext in {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif", "heic"}:
        return DocDecision(True, "image", "image document")

    # ── arbitrary documents: whitelist / blocklist ─────────────────────────
    if ext in BLOCKED_DOC_EXTENSIONS:
        return DocDecision(False, "document", f"blocked executable/code type (.{ext})")
    if ext in SAFE_DOC_EXTENSIONS:
        return DocDecision(True, "document", f"safe document (.{ext})")

    # Unknown type → conservative skip (never pull an unknown executable).
    return DocDecision(False, "document", f"unknown/unsafe type skipped (ext={ext!r} mime={mime_l!r})")


__all__ = [
    "classify_document",
    "DocDecision",
    "SAFE_DOC_EXTENSIONS",
    "BLOCKED_DOC_EXTENSIONS",
]
