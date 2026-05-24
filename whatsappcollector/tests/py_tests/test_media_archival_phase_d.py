import importlib
import sys
from pathlib import Path


MEDIA_ROOT = Path(__file__).resolve().parents[2] / "services" / "media_archival"
if str(MEDIA_ROOT) not in sys.path:
    sys.path.insert(0, str(MEDIA_ROOT))


def load_media_module(name: str):
    return importlib.import_module(f"media_archival.{name}")


def test_sanitize_jid_and_extension_mapping():
    downloader = load_media_module("downloader")

    assert downloader.sanitize_jid("12345-678@g.us") == "12345_678_g_us"
    assert downloader.mime_to_extension("image/jpeg") == ".jpg"
    assert downloader.mime_to_extension("application/pdf") == ".pdf"


def test_build_paths_uses_sanitized_jid():
    from pathlib import Path

    downloader = load_media_module("downloader")

    by_id, by_message = downloader.build_paths(Path("/tmp/media"), "12345-678@g.us", "sha123", "msg-1", "image/jpeg")

    assert by_id.parts[-2:] == ("by_id", "sha123.jpg")
    assert by_message.parts[-3:] == ("by_message", "12345_678_g_us", "msg-1.jpg")


def test_cleanup_pruning_gate_uses_min_cursor():
    cleanup = load_media_module("cleanup")

    assert cleanup.should_delete_media_file(10, 11) is True
    assert cleanup.should_delete_media_file(11, 11) is False
    assert cleanup.should_delete_media_file(12, 11) is False


def test_extract_media_metadata_finds_nested_payload():
    downloader = load_media_module("downloader")

    payload: dict[str, object] = {
        "message": {
            "imageMessage": {
                "mimetype": "image/jpeg",
                "fileSha256": "abc123",
                "fileLength": 1234,
            }
        }
    }

    meta = downloader.extract_media_metadata(payload)
    assert meta["mimetype"] == "image/jpeg"
    assert meta["fileSha256"] == "abc123"