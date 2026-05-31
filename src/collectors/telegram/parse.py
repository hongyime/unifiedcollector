"""Pure message-parsing helpers for the telegram collector.

Extracted from the collector class (STAGE 2 of the per-package refactor). These
classify a Telethon ``message`` object and derive a stable file id/extension.
They only READ the passed ``message`` (via getattr) and module-level constants —
no ``self``, no I/O — so they unit-test against lightweight fakes. The collector
keeps thin instance-method shims and re-imports the map / ext_from_mime for
back-compat.
"""
from __future__ import annotations

_MIME_EXT_MAP = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/mpeg": "mpeg",
    "video/quicktime": "mov",
    "video/webm": "webm",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/aac": "aac",
    "audio/flac": "flac",
    "application/pdf": "pdf",
    "application/zip": "zip",
    "application/x-tgsticker": "tgs",
    "image/vnd.djvu": "djvu",
    "text/plain": "txt",
}


def ext_from_mime(mime_type):
    if not mime_type:
        return None
    return _MIME_EXT_MAP.get(mime_type.lower())


def detect_message_type(message) -> str:
    """Return message_type string -- ported from realtime_worker."""
    if getattr(message, "photo", None) is not None:
        return "photo"
    video = getattr(message, "video", None)
    if video is not None:
        if getattr(video, "round_message", False):
            return "circle_video"
        return "video"
    if getattr(message, "audio", None) is not None:
        return "audio"
    if getattr(message, "voice", None) is not None:
        return "voice"
    if getattr(message, "document", None) is not None:
        return "document"
    if getattr(message, "sticker", None) is not None:
        return "sticker"
    if getattr(message, "poll", None) is not None:
        return "poll"
    if (
        getattr(message, "geo", None) is not None
        or getattr(message, "geo_live", None) is not None
    ):
        return "location"
    if getattr(message, "contact", None) is not None:
        return "contact"
    if getattr(message, "action", None) is not None:
        return "service"
    return "text"


def extract_file_info(message) -> tuple:
    """Return (file_unique_id, None, ext) -- ported from realtime_worker.

    file_unique_id derives from the Telethon-native object ID
    (photo.id or document.id), which is stable + unique across
    Telegram. Returns (None, None, None) if the message has no
    downloadable media.
    """
    photo = getattr(message, "photo", None)
    if photo is not None:
        fuid = getattr(photo, "id", None)
        return (str(fuid) if fuid is not None else None, None, "jpg")
    video = getattr(message, "video", None)
    if video is not None:
        ext = ext_from_mime(getattr(video, "mime_type", None)) or "mp4"
        fuid = getattr(video, "id", None)
        return (str(fuid) if fuid is not None else None, None, ext)
    audio = getattr(message, "audio", None)
    if audio is not None:
        ext = ext_from_mime(getattr(audio, "mime_type", None)) or "mp3"
        fuid = getattr(audio, "id", None)
        return (str(fuid) if fuid is not None else None, None, ext)
    voice = getattr(message, "voice", None)
    if voice is not None:
        ext = ext_from_mime(getattr(voice, "mime_type", None)) or "ogg"
        fuid = getattr(voice, "id", None)
        return (str(fuid) if fuid is not None else None, None, ext)
    sticker = getattr(message, "sticker", None)
    if sticker is not None:
        mime = getattr(sticker, "mime_type", "") or ""
        ext = "tgs" if "tgsticker" in mime else "webp"
        fuid = getattr(sticker, "id", None)
        return (str(fuid) if fuid is not None else None, None, ext)
    document = getattr(message, "document", None)
    if document is not None:
        mime = getattr(document, "mime_type", None)
        ext = ext_from_mime(mime)
        if not ext:
            for attr in getattr(document, "attributes", []):
                fname = getattr(attr, "file_name", None)
                if fname and "." in fname:
                    ext = fname.rsplit(".", 1)[-1].lower()
                    break
        ext = ext or "bin"
        fuid = getattr(document, "id", None)
        return (str(fuid) if fuid is not None else None, None, ext)
    return (None, None, None)
