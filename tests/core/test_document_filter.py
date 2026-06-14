"""Tests for src/core/document_filter.py — Tier 3 doc/sticker classification."""
from __future__ import annotations

from src.core.document_filter import (
    BLOCKED_DOC_EXTENSIONS,
    SAFE_DOC_EXTENSIONS,
    classify_document,
)


def test_safe_documents_downloaded():
    for mime, name in [
        ("application/pdf", "report.pdf"),
        ("application/msword", "memo.doc"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx"),
        ("application/vnd.ms-excel", "data.xls"),
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "deck.pptx"),
        (None, "notes.txt"),
        (None, "list.md"),
        (None, "contact.vcf"),
    ]:
        d = classify_document(mime, name)
        assert d.download, f"expected download for {name}: {d.reason}"
        assert d.content_type in ("document", "image")


def test_executables_and_code_skipped():
    for mime, name in [
        ("application/x-msdownload", "setup.exe"),
        (None, "lib.dll"),
        (None, "app.apk"),
        (None, "installer.msi"),
        (None, "script.py"),
        (None, "bundle.js"),
        (None, "Main.java"),
        (None, "run.sh"),
        (None, "deploy.ps1"),
        (None, "macro.bat"),
    ]:
        d = classify_document(mime, name)
        assert not d.download, f"expected SKIP for {name}: {d.reason}"


def test_audio_is_stored():
    d = classify_document("audio/ogg", "voice.ogg")
    assert d.download and d.content_type == "audio"
    d2 = classify_document("audio/mpeg", "song.mp3")
    assert d2.download and d2.content_type == "audio"


def test_video_document_is_media():
    d = classify_document("video/mp4", "clip.mp4")
    assert d.download and d.content_type == "video"


def test_static_sticker_kept_animated_skipped():
    static = classify_document("image/webp", None, is_sticker=True)
    assert static.download and static.content_type == "sticker"

    tgs = classify_document("application/x-tgsticker", None, is_sticker=True, is_animated=True)
    assert not tgs.download

    webm = classify_document("video/webm", None, is_sticker=True, is_animated=True)
    assert not webm.download

    # .tgs detected even without the sticker flag (MIME alone)
    tgs2 = classify_document("application/x-tgsticker", "anim.tgs")
    assert not tgs2.download


def test_unknown_type_conservatively_skipped():
    d = classify_document("application/x-weird-binary", "thing.zzz")
    assert not d.download


def test_image_document_downloaded():
    d = classify_document("image/jpeg", "photo.jpg")
    assert d.download
    assert d.content_type in ("image", "document")


def test_whitelist_blocklist_disjoint():
    # A type can't be both safe and blocked.
    assert not (SAFE_DOC_EXTENSIONS & BLOCKED_DOC_EXTENSIONS)
