"""Unit tests for telegram pure message-parse helpers (STAGE 2 safety net)."""
from types import SimpleNamespace as NS

from src.collectors.telegram.parse import (
    detect_message_type,
    extract_file_info,
    ext_from_mime,
    _MIME_EXT_MAP,
)


def _msg(**kw):
    # default all media attrs to None, override via kwargs
    base = dict(photo=None, video=None, audio=None, voice=None, document=None,
                sticker=None, poll=None, geo=None, geo_live=None, contact=None,
                action=None)
    base.update(kw)
    return NS(**base)


def test_detect_message_type_priority():
    assert detect_message_type(_msg(photo=NS(id=1))) == "photo"
    assert detect_message_type(_msg(video=NS(id=2, round_message=False))) == "video"
    assert detect_message_type(_msg(video=NS(id=2, round_message=True))) == "circle_video"
    assert detect_message_type(_msg(voice=NS(id=3))) == "voice"
    assert detect_message_type(_msg(document=NS(id=4))) == "document"
    assert detect_message_type(_msg(poll=NS())) == "poll"
    assert detect_message_type(_msg(geo=NS())) == "location"
    assert detect_message_type(_msg(action=NS())) == "service"
    assert detect_message_type(_msg()) == "text"


def test_ext_from_mime():
    assert ext_from_mime("image/jpeg") == "jpg"
    assert ext_from_mime("VIDEO/MP4") == "mp4"   # case-insensitive
    assert ext_from_mime("application/unknown") is None
    assert ext_from_mime(None) is None
    assert "audio/ogg" in _MIME_EXT_MAP


def test_extract_file_info_photo():
    fuid, _, ext = extract_file_info(_msg(photo=NS(id=999)))
    assert fuid == "999" and ext == "jpg"


def test_extract_file_info_video_mime():
    fuid, _, ext = extract_file_info(_msg(video=NS(id=5, mime_type="video/webm")))
    assert fuid == "5" and ext == "webm"


def test_extract_file_info_document_fallback_to_filename_ext():
    doc = NS(id=7, mime_type=None, attributes=[NS(file_name="report.PDF")])
    fuid, _, ext = extract_file_info(_msg(document=doc))
    assert fuid == "7" and ext == "pdf"


def test_extract_file_info_none():
    assert extract_file_info(_msg()) == (None, None, None)
