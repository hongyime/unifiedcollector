import logging
import os
import struct
import sys
import zlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

# Add parent directory and src folder to sys.path
_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root / 'src'))
sys.path.insert(0, str(_root))

import config as config_module
import downloader as media_downloader_module
from downloader import MediaDownloader


def make_png(width: int, height: int, padding_bytes: int = 20480) -> bytes:
    """Build a minimal PNG-like byte stream with configurable dimensions."""
    signature = b'\x89PNG\r\n\x1a\n'
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = (
        len(ihdr_data).to_bytes(4, 'big') +
        b'IHDR' +
        ihdr_data +
        zlib.crc32(b'IHDR' + ihdr_data).to_bytes(4, 'big')
    )
    payload = (b'A' * max(padding_bytes, 1))
    idat = (
        len(payload).to_bytes(4, 'big') +
        b'IDAT' +
        payload +
        zlib.crc32(b'IDAT' + payload).to_bytes(4, 'big')
    )
    iend = (
        (0).to_bytes(4, 'big') +
        b'IEND' +
        b'' +
        zlib.crc32(b'IEND').to_bytes(4, 'big')
    )
    return signature + ihdr + idat + iend


class FakeResponse:
    def __init__(self, body: bytes, status_code: int = 200):
        self.body = body
        self.status_code = status_code
        self.headers = {'content-length': str(len(body))}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 8192):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index:index + chunk_size]


@pytest.fixture
def downloader(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    downloads_dir = tmp_path / "downloads"
    data_dir.mkdir()
    downloads_dir.mkdir()

    downloaded_media_file = data_dir / "downloaded_media.json"
    verification_log_file = data_dir / "download_verification.log"
    test_db_file = data_dir / "lemon8_toolkit_test.db"

    logger = logging.getLogger("lemon8.media_downloader")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    # After the config.X refactor, patch config directly — changes propagate
    # automatically to downloader.py since it reads config.X at call time.
    monkeypatch.setattr(config_module, "LEMON8_DB_FILE", str(test_db_file))
    monkeypatch.setattr(config_module, "DOWNLOADED_MEDIA_FILE", str(downloaded_media_file))
    monkeypatch.setattr(config_module, "DOWNLOAD_VERIFICATION_LOG_FILE", str(verification_log_file))
    monkeypatch.setattr(config_module, "get_downloads_directory", lambda: str(downloads_dir))
    monkeypatch.setattr(config_module, "MIN_DELAY", 0)
    monkeypatch.setattr(config_module, "MIN_IMAGE_FILE_SIZE_BYTES", 100)
    monkeypatch.setattr(config_module, "MIN_PROFILE_IMAGE_FILE_SIZE_BYTES", 50)

    return MediaDownloader(auto_save=False)


def test_is_already_downloaded(downloader):
    url = "https://example.com/media.mp4"
    url_hash = downloader._get_url_hash(url)

    assert downloader.is_already_downloaded(url) is False

    downloader.mark_as_downloaded(url)
    assert downloader.is_already_downloaded(url) is True
    assert downloader.is_already_downloaded(url_hash) is True


def test_save_and_load(downloader):
    url = "https://example.com/media2.mp4"
    downloader.mark_as_downloaded(url)
    downloader.save()

    new_downloader = MediaDownloader(auto_save=False)
    assert new_downloader.is_already_downloaded(url) is True


def test_get_filename_from_url(downloader):
    assert downloader._get_filename_from_url("https://example.com/foo.mp4") == "foo.mp4"
    assert downloader._get_filename_from_url("https://example.com/foo") == (
        f"media_{downloader._get_url_hash('https://example.com/foo')[:12]}.mp4"
    )
    assert downloader._get_filename_from_url("https://example.com/foo.jpg?v=1") == "foo.jpg"


def test_enhance_image_url_rewrites_common_patterns(downloader):
    url = (
        "https://example.com/o8UprABH~tplv-sdweummd6v-shrink_640_0_q50.webp"
        "?w=200&h=150&q=50"
    )

    candidates = downloader._enhance_image_url(url)

    assert candidates[-1] == url
    assert "image.webp" in candidates[0]
    assert "w=2160" in candidates[0]
    assert "h=2160" in candidates[0]
    assert "q=100" in candidates[0]


def test_build_prefixed_filename_sanitizes_username_and_resolves_conflicts(downloader, tmp_path):
    filename = downloader._build_prefixed_filename("@Jane Doe/Dev", "photo.jpg")
    assert filename == "Jane_Doe_Dev_photo.jpg"

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / filename).write_bytes(b'exists')

    unique_filename = downloader._ensure_unique_filename(str(media_dir), filename)
    assert unique_filename == "Jane_Doe_Dev_photo_1.jpg"


def test_compare_image_quality_detects_improvement(downloader):
    original_url = (
        "https://example.com/photo~tplv-sdweummd6v-shrink_640_0_q50.webp"
        "?w=640&h=640&q=50"
    )
    enhanced_url = (
        "https://example.com/photo~tplv-sdweummd6v-image.webp"
        "?w=2160&h=2160&q=100"
    )

    comparison = downloader._compare_image_quality(
        original_url,
        enhanced_url,
        original_info={'file_size_bytes': 2048, 'width': 640, 'height': 640},
        enhanced_info={'file_size_bytes': 8192, 'width': 2160, 'height': 2160},
    )

    assert comparison['is_higher_quality'] is True
    assert comparison['shrink_removed'] is True
    assert comparison['width_better'] is True
    assert comparison['quality_better'] is True


def test_download_media_uses_high_quality_candidate_and_username_prefix(downloader):
    original_url = (
        "https://example.com/photo~tplv-sdweummd6v-shrink_640_0_q50.webp"
        "?w=640&h=640&q=50"
    )
    downloader.session = MagicMock()
    downloader.session.get.return_value = FakeResponse(make_png(1200, 1200))

    save_path = downloader.download_media(original_url, 'user', '@Test User')

    assert save_path is not None
    assert Path(save_path).name.startswith("Test_User_")
    first_requested_url = downloader.session.get.call_args_list[0].args[0]
    assert first_requested_url == downloader._enhance_image_url(original_url)[0]


def test_download_media_falls_back_when_high_quality_version_is_unavailable(downloader):
    original_url = "https://example.com/photo-small.webp"
    downloader._enhance_image_url = lambda url: ["https://example.com/photo-large.webp", original_url]
    downloader.session = MagicMock()
    downloader.session.get.side_effect = [
        requests.RequestException("high quality failed"),
        FakeResponse(make_png(900, 900)),
    ]

    save_path = downloader.download_media(original_url, 'user', 'fallback_user')

    assert save_path is not None
    assert downloader.session.get.call_count == 2
    assert Path(save_path).name.startswith("fallback_user_")


def test_download_media_accepts_standard_feed_image_dimensions(downloader, monkeypatch):
    original_url = "https://example.com/feed-large.webp?w=640&h=853&q=50"
    downloader._enhance_image_url = lambda url: [url]
    downloader.session = MagicMock()
    downloader.session.get.return_value = FakeResponse(make_png(640, 853, padding_bytes=16 * 1024))
    monkeypatch.setattr(config_module, "MIN_IMAGE_WIDTH", 320)
    monkeypatch.setattr(config_module, "MIN_IMAGE_HEIGHT", 320)
    monkeypatch.setattr(config_module, "MIN_IMAGE_FILE_SIZE_BYTES", 8 * 1024)

    save_path = downloader.download_media(original_url, 'feed', 'foryou', filename_prefix='feed_author')

    assert save_path is not None
    assert Path(save_path).name.startswith("feed_author_")


def test_download_media_rejects_images_that_fail_quality_thresholds(downloader):
    original_url = "https://example.com/tiny-thumb.webp?w=120&h=120&q=40"
    downloader._enhance_image_url = lambda url: [url]
    downloader.session = MagicMock()
    downloader.session.get.return_value = FakeResponse(make_png(120, 120, padding_bytes=16))

    save_path = downloader.download_media(original_url, 'user', 'tiny_user')

    assert save_path is None
    user_dir = Path(downloader._get_downloads_dir()) / "tiny_user"
    assert list(user_dir.glob("*")) == []


def test_download_media_handles_network_failures(downloader):
    original_url = "https://example.com/broken.webp"
    downloader._enhance_image_url = lambda url: [url]
    downloader.session = MagicMock()
    downloader.session.get.side_effect = requests.RequestException("network down")

    save_path = downloader.download_media(original_url, 'user', 'network_user')

    assert save_path is None


def test_download_multiple_media_uses_descriptor_username_for_prefix(downloader):
    downloader.session = MagicMock()
    downloader.session.get.return_value = FakeResponse(make_png(1200, 1200))
    media_items = [
        {
            "url": "https://example.com/feed-large.webp?w=640&h=853&q=50",
            "username": "actual_author",
            "is_profile_photo": False,
        }
    ]

    results = downloader.download_multiple_media(media_items, 'feed', 'foryou')
    save_path = results[media_items[0]["url"]]

    assert save_path is not None
    assert Path(save_path).name.startswith("actual_author_")
    assert Path(save_path).parent.name == "actual_author"


def test_profile_photo_uses_profile_thresholds_when_enabled(downloader):
    original_url = "https://example.com/user-avatar/profile_owner_120x120.jpg"
    downloader._enhance_image_url = lambda url: [url]
    downloader.session = MagicMock()
    downloader.session.get.return_value = FakeResponse(make_png(120, 120, padding_bytes=64))

    save_path = downloader.download_media(
        original_url,
        'user',
        'profile_owner',
        is_profile_photo=True,
    )

    assert save_path is not None
    assert Path(save_path).name.startswith("profile_owner_")
