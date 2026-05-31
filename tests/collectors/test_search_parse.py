"""Unit tests for search pure URL-classification helpers (STAGE 2 safety net)."""
from src.collectors.search.parse import (
    is_content_url,
    CONTENT_EXTENSIONS,
    ICON_KEYWORDS,
)


def test_content_url_accepts_image_and_pdf():
    assert is_content_url("http://x.com/photo.jpg")
    assert is_content_url("http://x.com/doc.pdf")
    assert is_content_url("https://y.org/a/b/IMG_001.JPEG")


def test_content_url_rejects_non_content_ext():
    assert not is_content_url("http://x.com/page.html")
    assert not is_content_url("http://x.com/style.css")
    assert not is_content_url("http://x.com/noext")


def test_content_url_rejects_icon_keywords():
    assert not is_content_url("http://x.com/favicon.png")
    assert not is_content_url("http://x.com/assets/logo.png")
    assert not is_content_url("http://x.com/sprite-sheet.png")


def test_constants_intact():
    assert ".pdf" in CONTENT_EXTENSIONS
    assert "favicon" in ICON_KEYWORDS
