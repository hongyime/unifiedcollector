"""Unit tests for lemon8 pure URL/username helpers (STAGE 2 safety net)."""
from src.collectors.lemon8.parse import (
    normalize_username,
    clean_media_url,
    is_valid_media_url,
    is_small_image,
    is_profile_photo_url,
)


def test_normalize_username():
    assert normalize_username("  @Bryan.Seah_234 ") == "bryan.seah_234"
    assert normalize_username("@@@") is None
    assert normalize_username(None) is None
    assert normalize_username(123) is None


def test_clean_media_url():
    assert clean_media_url("a&amp;b") == "a&b"
    assert clean_media_url("") == ""


def test_is_valid_media_url():
    assert is_valid_media_url("https://x.com/post/a.jpg")
    assert is_valid_media_url("https://x.com/item/v.mp4")
    assert not is_valid_media_url("https://x.com/favicon.ico")
    assert not is_valid_media_url("ftp://x.com/a.jpg")
    assert not is_valid_media_url("https://x.com/style.css")
    assert not is_valid_media_url("")


def test_is_small_image():
    assert is_small_image("https://x.com/thumb_100x100.jpg")
    assert is_small_image("https://x.com/a_640x200.jpg")  # min dim < 250
    assert is_small_image("https://x.com/avatar/abc.jpg")
    assert not is_small_image("https://x.com/photo_1080x1920.jpg")
    assert not is_small_image("")


def test_is_profile_photo_url():
    assert is_profile_photo_url("https://x.com/user-avatar/abc")
    assert is_profile_photo_url("https://x.com/profile-photo/abc")
    assert not is_profile_photo_url("https://x.com/post/abc.jpg")
    assert not is_profile_photo_url("")
